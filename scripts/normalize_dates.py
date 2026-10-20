# Copyright 2026 Open Reaction Database Project Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Rewrite every DateTime in ISO 8601, keeping its time.

ord-schema accepts a ``DateTime`` only as a date, ``YYYY-MM-DD``, or a time,
``YYYY-MM-DDTHH:MM:SS[.ffffff]``, optionally followed by ``Z`` or ``±HH:MM``. A
value already in one of those shapes is left as written. Every other value keeps
its wall-clock time and its fraction of a second, and gains no offset its source
did not record.

Slash dates are read in the order their own numbers prove; run
``rewrite_ambiguous_dates.py`` first, since a slash date that proves neither
order is refused here. Each reaction with a rewritten value gains one
``record_modified`` event listing them.
"""

import argparse
import datetime
import logging
import re
from pathlib import Path

from ord_schema import parquet
from ord_schema.proto import reaction_pb2
from rewrite_ambiguous_dates import (
    describe,
    is_ambiguous,
    iter_date_times,
    make_event,
    to_iso,
    witnessed_day_first,
)

logger = logging.getLogger(__name__)

# The first line of each event's details; one line per rewritten value follows.
DETAILS = (
    "Rewrote dates in ISO 8601; see "
    "https://github.com/open-reaction-database/ord-data/pull/303"
)

# The shapes ord-schema's validations accept, copied because ord-data locks a release
# that predates them. fromisoformat reads an offset of "+00:60" as one hour, so the
# minutes are bounded here.
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:[0-5]\d)?"
)
_CTIME = "%a %b %d %H:%M:%S %Y"
# ISO 8601 with a space before the time, as str(datetime) writes it.
_SPACED = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)")


def is_valid(value: str) -> bool:
    """Return whether ord-schema accepts a DateTime value as written."""
    try:
        if _DATE.fullmatch(value):
            datetime.date.fromisoformat(value)
            return True
        if _TIME.fullmatch(value):
            datetime.datetime.fromisoformat(value)
            return True
    except ValueError:
        return False
    return False


def iso_form(value: str) -> str:
    """Return a zone-less DateTime value in ISO 8601, with the same wall-clock time.

    Args:
        value: A slash date that proves its own order, a ctime stamp, or an ISO
            8601 time with a space before the time.

    Returns:
        ``YYYY-MM-DD`` for a date alone, else ``YYYY-MM-DDTHH:MM:SS`` with any
        fraction of a second the source wrote, digit for digit.

    Raises:
        ValueError: If ``value`` is an ambiguous slash date or in no known shape.
    """
    if is_ambiguous(value):
        raise ValueError(f"{value!r} does not prove its day and month order")
    day_first = witnessed_day_first(value)
    if day_first is not None:
        return to_iso(value, day_first=day_first)
    if spaced := _SPACED.fullmatch(value):
        return f"{spaced[1]}T{spaced[2]}"
    try:
        # DTZ007: a ctime stamp carries no offset, and none is added.
        return datetime.datetime.strptime(value, _CTIME).isoformat()  # noqa: DTZ007
    except ValueError:
        pass
    raise ValueError(f"no known shape matches {value!r}")


def normalize_reaction(reaction: reaction_pb2.Reaction) -> list[tuple[str, str, str]]:
    """Rewrite a reaction's DateTime values that ord-schema does not accept, in place.

    Args:
        reaction: The reaction to modify.

    Returns:
        ``(path, old value, new value)`` for each value rewritten, in the order
        ``iter_date_times`` yields them.

    Raises:
        ValueError: If a value is in no shape this can read.
    """
    changes = []
    for path, date_time in iter_date_times(reaction):
        old = date_time.value
        if is_valid(old):
            continue
        new = iso_form(old)
        date_time.value = new
        changes.append((path, old, new))
    return changes


def normalize_dataset(
    path: Path, event: reaction_pb2.RecordEvent, *, dry_run: bool = False
) -> tuple[int, int]:
    """Rewrite a dataset file's DateTime values that ord-schema does not accept.

    A dataset with none is not written.

    Args:
        path: The Parquet dataset.
        event: Appended to the ``record_modified`` events of each reaction with
            a rewritten value, and of no other, with ``DETAILS`` and one line
            per rewritten value as its details.
        dry_run: Check and count without writing.

    Returns:
        The numbers of reactions and of values rewritten, or that would be.

    Raises:
        ValueError: If a value is in no shape this can read; nothing is written.
        RuntimeError: If the written file does not read back with the same
            scalars and reaction IDs and every value accepted. The file is left in
            place for inspection; the original is in git.
    """
    view = parquet.DatasetView(path)
    # A first pass rewrites nothing, so a value it cannot read fails before any
    # file is touched.
    reactions = values = 0
    for reaction in view.reactions:
        changes = normalize_reaction(reaction)
        if changes:
            reactions += 1
            values += len(changes)
    if not values or dry_run:
        return reactions, values
    reaction_ids = list(view.iter_reaction_ids())
    with parquet.DatasetWriter(
        path,
        name=view.name,
        description=view.description,
        dataset_id=view.dataset_id,
    ) as writer:
        for reaction in view.reactions:
            changes = normalize_reaction(reaction)
            if changes:
                modified = reaction.provenance.record_modified.add()
                modified.CopyFrom(event)
                modified.details = describe(changes, DETAILS)
            writer.write(reaction)
    written = parquet.DatasetView(path)
    if (written.name, written.description, written.dataset_id) != (
        view.name,
        view.description,
        view.dataset_id,
    ) or list(written.iter_reaction_ids()) != reaction_ids:
        raise RuntimeError(f"{path} does not read back as planned")
    for reaction in written.reactions:
        if any(not is_valid(dt.value) for _, dt in iter_date_times(reaction)):
            raise RuntimeError(f"{path} still holds a DateTime ord-schema rejects")
    return reactions, values


def main() -> None:
    """Rewrite the DateTime values in every dataset under data/."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root containing data/ (default: parent of this script's dir).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and count the values to rewrite; do not write any files.",
    )
    parser.add_argument(
        "--email", required=True, help="Email of the person making the change."
    )
    parser.add_argument("--username", default="", help="Their GitHub username.")
    parser.add_argument("--name", default="", help="Their full name.")
    parser.add_argument("--organization", default="", help="Their organization.")
    args = parser.parse_args()
    if not (args.username or args.name):
        parser.error("validation requires --username or --name")

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    person = reaction_pb2.Person(
        username=args.username,
        name=args.name,
        organization=args.organization,
        email=args.email,
    )
    event = make_event(person, datetime.datetime.now(datetime.UTC))
    paths = sorted(args.repo_root.glob("data/*/*.parquet"))
    total_reactions = total_values = 0
    for path in paths:
        reactions, values = normalize_dataset(path, event, dry_run=args.dry_run)
        if values:
            logger.info(f"{path.relative_to(args.repo_root)}  {reactions}  {values}")
            total_reactions += reactions
            total_values += values
    verb = "would rewrite" if args.dry_run else "rewrote"
    logger.info(
        f"{verb} {total_values} values in {total_reactions} reactions across "
        f"{len(paths)} datasets"
    )


if __name__ == "__main__":
    main()
