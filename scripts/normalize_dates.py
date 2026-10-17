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

"""Rewrite every DateTime as a date or a time with its UTC offset.

ord-schema accepts a ``DateTime`` only as a date, ``YYYY-MM-DD``, or a time with
the offset it was written under, ``YYYY-MM-DDTHH:MM:SS[.ffffff]`` then ``Z`` or
``±HH:MM``. A value already in one of those shapes is left as written. Of the
rest, only the submission pipeline's events have a known zone: their ``ctime``
stamps come from ``datetime.now(UTC)``, so those become the same time with
``+00:00``. Every other value is written as its date alone, the date its
wall-clock time falls on, since a time with no zone names no particular instant.

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
    to_date,
    witnessed_day_first,
)

logger = logging.getLogger(__name__)

# The first line of each event's details; one line per rewritten value follows.
DETAILS = (
    "Rewrote dates as a date or a UTC time with its offset; see "
    "https://github.com/open-reaction-database/ord-data/pull/303"
)

# What ord_schema.updates stamps on a reaction it modifies.
PIPELINE_EMAIL = "github-actions@github.com"
PIPELINE_DETAILS = "Automatic updates from the submission pipeline."

# The shapes ord-schema's validations accept, copied because ord-data locks a release
# that predates them. fromisoformat reads an offset of "+00:60" as one hour, so the
# minutes are bounded here.
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:[0-5]\d)"
)
_CTIME = "%a %b %d %H:%M:%S %Y"
# The zone-less shapes the corpus holds besides slash dates and ctime.
_NAIVE = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


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


def pipeline_time(value: str) -> str:
    """Return a pipeline event's ctime stamp as the same UTC time with its offset.

    Args:
        value: The stamp, as ``datetime.now(UTC).ctime()`` wrote it.

    Returns:
        ``YYYY-MM-DDTHH:MM:SS+00:00``.

    Raises:
        ValueError: If ``value`` is not a ctime stamp.
    """
    parsed = datetime.datetime.strptime(value, _CTIME).replace(tzinfo=datetime.UTC)
    return parsed.isoformat()


def date_of(value: str) -> str:
    """Return the date a zone-less DateTime value's wall-clock time falls on.

    Args:
        value: A slash date that proves its own order, a ctime stamp, or an ISO
            8601 time with no offset, with a space or a ``T`` before the time.

    Returns:
        ``YYYY-MM-DD``.

    Raises:
        ValueError: If ``value`` is an ambiguous slash date or in no known shape.
    """
    if is_ambiguous(value):
        raise ValueError(f"{value!r} does not prove its day and month order")
    day_first = witnessed_day_first(value)
    if day_first is not None:
        return to_date(value, day_first=day_first)
    for shape in (_CTIME, *_NAIVE):
        try:
            # DTZ007: the value carries no offset, and only its date is kept.
            return datetime.datetime.strptime(value, shape).date().isoformat()  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError(f"no known shape matches {value!r}")


def _pipeline_paths(reaction: reaction_pb2.Reaction) -> set[str]:
    """Return the paths of the times on the pipeline's record_modified events."""
    return {
        f"provenance.record_modified[{index}].time"
        for index, event in enumerate(reaction.provenance.record_modified)
        if event.person.email == PIPELINE_EMAIL and event.details == PIPELINE_DETAILS
    }


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
    pipeline = _pipeline_paths(reaction)
    changes = []
    for path, date_time in iter_date_times(reaction):
        old = date_time.value
        if is_valid(old):
            continue
        new = pipeline_time(old) if path in pipeline else date_of(old)
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
