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

"""Rewrite slash dates that leave the day and month order open as ISO 8601.

A ``DateTime`` written ``NN/NN/NNNN`` with both leading numbers at most twelve
names two dates, and which one the depositor meant is a property of the dataset
rather than of the value. ``DAY_FIRST`` records the order for every dataset that
holds such values, as settled in the ord-logbook entry "Date and time formats
across the corpus" (2026-09-03); the three day-first ones are among the six in
ord-schema's ``artifacts.projection._DAY_FIRST``.

Only those values are rewritten: ``YYYY-MM-DD`` where the source carried no
time, ``YYYY-MM-DDTHH:MM:SS`` otherwise, with no offset since the source recorded
none. A slash date left standing has a leading number above twelve, so it reads
one way under either order. Each reaction with a rewritten value gains one
``record_modified`` event listing them.
"""

import argparse
import collections
import datetime
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

import pyarrow.parquet as pq
from google.protobuf import message as message_lib
from google.protobuf.descriptor import FieldDescriptor
from ord_schema import parquet
from ord_schema.proto import reaction_pb2

logger = logging.getLogger(__name__)

# Whether each dataset holding ambiguous slash dates writes them day first.
DAY_FIRST = {
    "ord_dataset-00005539a1e04c809a9a78647bea649c": False,
    "ord_dataset-0c75d67751634f0594b24b9f498b77c2": False,
    "ord_dataset-2be11f57f3304e678ea8469baf6dd1bc": True,
    "ord_dataset-35a5a513f1dd44a3a97c88da99f81a00": False,
    "ord_dataset-3b5db90e337942ea886b8f5bc5e3aa72": False,
    "ord_dataset-46ff9a32d9e04016b9380b1b1ef949c3": False,
    "ord_dataset-4d431564f3ef4e9c91d8da5836f4eae6": False,
    "ord_dataset-5c9a10329a8a48968d18879a48bb8ab2": True,
    "ord_dataset-5e8318f0dda04b398c14f4c3adfeb32c": True,
    "ord_dataset-675eddcaa6674ce3ae61e79bbc1e1c08": False,
    "ord_dataset-89b083710e2d441aa0040c361d63359f": False,
    "ord_dataset-cbcc4048add7468e850b6ec42549c70d": False,
    "ord_dataset-d26118acda314269becc35db5c22dc59": False,
    "ord_dataset-d92976309c3a48a3a64a4cf5e7048086": False,
}

# The first line of each event's details; one line per rewritten value follows.
DETAILS = (
    "Rewrote ambiguous dates as ISO 8601; see "
    "https://github.com/open-reaction-database/ord-data/pull/303"
)

SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/")
# A reaction whose serialized bytes hold no slash date has none to rewrite or to
# witness an order, so it is not deserialized.
_SLASH_BYTES = re.compile(rb"(?<!\d)\d{1,2}/\d{1,2}/\d")
# What follows the day and month in each slash shape the corpus holds.
_TAILS = ("/%Y, %I:%M:%S %p", "/%Y, %H:%M:%S", "/%Y %H:%M:%S", "/%Y")
_DATE_ONLY_TAIL = "/%Y"


def leading_numbers(value: str) -> tuple[int, int] | None:
    """Return the two leading numbers of a slash date, or None for other values."""
    match = SLASH_DATE.match(value)
    if match is None:
        return None
    return int(match[1]), int(match[2])


def is_ambiguous(value: str) -> bool:
    """Return whether a value is a slash date whose day and month could swap."""
    numbers = leading_numbers(value)
    return numbers is not None and max(numbers) <= 12


def witnessed_day_first(value: str) -> bool | None:
    """Return the order a slash date proves, or None if it proves neither.

    Args:
        value: A DateTime value.

    Returns:
        True when the first number exceeds twelve, False when the second does,
        and None otherwise, including for values that are not slash dates.
    """
    numbers = leading_numbers(value)
    if numbers is None:
        return None
    first, second = numbers
    if first > 12:
        return True
    if second > 12:
        return False
    return None


def to_iso(value: str, *, day_first: bool) -> str:
    """Return the ISO 8601 form of a slash date read in the given order.

    Args:
        value: A slash date in one of the shapes ``_TAILS`` covers.
        day_first: Whether the first number is the day.

    Returns:
        ``YYYY-MM-DD`` for a value without a time, else ``YYYY-MM-DDTHH:MM:SS``.

    Raises:
        ValueError: If no shape matches ``value``, including one with an offset.
    """
    order = "%d/%m" if day_first else "%m/%d"
    for tail in _TAILS:
        try:
            # DTZ007: none of these shapes carries an offset, and the ISO form
            # records the same wall-clock time without inventing one.
            parsed = datetime.datetime.strptime(value, order + tail)  # noqa: DTZ007
        except ValueError:
            continue
        if tail == _DATE_ONLY_TAIL:
            return parsed.date().isoformat()
        return parsed.isoformat()
    raise ValueError(f"no slash date shape matches {value!r}")


def iter_date_times(
    message: message_lib.Message, path: str = ""
) -> Iterator[tuple[str, reaction_pb2.DateTime]]:
    """Yield every populated DateTime under ``message`` with its field path.

    A path names fields as the schema does, with ``[i]`` for a position in a
    repeated field and ``["key"]`` for a map key:
    ``outcomes[0].analyses["hplc"].instrument_last_calibrated``. Fields come in
    field number order and map keys in sorted order.

    Args:
        message: The message to walk.
        path: The path to ``message`` itself; empty for the root.

    Yields:
        ``(path, DateTime)`` pairs, depth first.
    """
    if isinstance(message, reaction_pb2.DateTime):
        yield path, message
        return
    for field, value in message.ListFields():
        if field.type != FieldDescriptor.TYPE_MESSAGE:
            continue
        name = f"{path}.{field.name}" if path else field.name
        if field.message_type.GetOptions().map_entry:
            children = [
                (f"{name}[{json.dumps(key)}]", value[key]) for key in sorted(value)
            ]
        elif field.label == FieldDescriptor.LABEL_REPEATED:
            children = [(f"{name}[{i}]", child) for i, child in enumerate(value)]
        else:
            children = [(name, value)]
        for child_path, child in children:
            yield from iter_date_times(child, child_path)


def _iter_slash_reactions(
    view: parquet.DatasetView,
) -> Iterator[reaction_pb2.Reaction]:
    """Yield the reactions in ``view`` whose bytes hold a slash date."""
    with pq.ParquetFile(view.path) as parquet_file:
        for batch in parquet_file.iter_batches(columns=["reaction"]):
            for blob in batch.column("reaction").to_pylist():
                if _SLASH_BYTES.search(blob):
                    yield reaction_pb2.Reaction.FromString(blob)


def scan(view: parquet.DatasetView) -> tuple[int, collections.Counter[bool]]:
    """Count a dataset's ambiguous slash dates, and the values proving each order.

    Args:
        view: The dataset; only reactions whose bytes hold a slash date are parsed.

    Returns:
        The number of ambiguous values, and how many values prove each order.
    """
    ambiguous = 0
    witnesses: collections.Counter[bool] = collections.Counter()
    for reaction in _iter_slash_reactions(view):
        for _, date_time in iter_date_times(reaction):
            if is_ambiguous(date_time.value):
                ambiguous += 1
            witnessed = witnessed_day_first(date_time.value)
            if witnessed is not None:
                witnesses[witnessed] += 1
    return ambiguous, witnesses


def rewrite_reaction(
    reaction: reaction_pb2.Reaction, *, day_first: bool
) -> list[tuple[str, str, str]]:
    """Rewrite a reaction's ambiguous slash dates in place.

    Args:
        reaction: The reaction to modify.
        day_first: Whether its dataset writes the day first.

    Returns:
        ``(path, old value, new value)`` for each value rewritten, in the order
        ``iter_date_times`` yields them.
    """
    changes = []
    for path, date_time in iter_date_times(reaction):
        if is_ambiguous(date_time.value):
            old = date_time.value
            date_time.value = to_iso(old, day_first=day_first)
            changes.append((path, old, date_time.value))
    return changes


def describe(changes: Iterable[tuple[str, str, str]], header: str = DETAILS) -> str:
    """Return an event's details: ``header``, then ``path: old -> new`` per line."""
    return "\n".join(
        [header, *(f"{path}: {old} -> {new}" for path, old, new in changes)]
    )


def make_event(
    person: reaction_pb2.Person, now: datetime.datetime
) -> reaction_pb2.RecordEvent:
    """Build the record_modified event a rewritten reaction gains, without details.

    Args:
        person: Who made the change. Validation requires an email, and one of a
            username, a name, or an ORCID.
        now: When, as an aware datetime. It is written in UTC with its offset,
            ``YYYY-MM-DDTHH:MM:SS+00:00``, so the zone is in the record.

    Returns:
        The event, with no details.

    Raises:
        ValueError: If ``now`` is naive, since its zone would be a guess.
    """
    if now.tzinfo is None:
        raise ValueError(f"{now} has no time zone")
    event = reaction_pb2.RecordEvent()
    event.person.CopyFrom(person)
    event.time.value = now.astimezone(datetime.UTC).isoformat(timespec="seconds")
    return event


def rewrite_dataset(
    path: Path,
    event: reaction_pb2.RecordEvent,
    *,
    day_first: Mapping[str, bool] = DAY_FIRST,
    dry_run: bool = False,
) -> int:
    """Rewrite a dataset file's ambiguous slash dates in place.

    A dataset with none is not written.

    Args:
        path: The Parquet dataset.
        event: Appended to the ``record_modified`` events of each reaction with
            a rewritten value, and of no other, with details from ``describe``.
        day_first: The settled order of each dataset that may hold ambiguous
            values.
        dry_run: Check and count without writing.

    Returns:
        The number of values rewritten, or that would be.

    Raises:
        ValueError: If the dataset holds ambiguous values but has no settled
            order, or its own values prove the other order.
        RuntimeError: If the written file does not read back with the same
            scalars and reaction IDs and no ambiguous value. The file is left in
            place for inspection; the original is in git.
    """
    view = parquet.DatasetView(path)
    ambiguous, witnesses = scan(view)
    if not ambiguous:
        return 0
    if view.dataset_id not in day_first:
        raise ValueError(
            f"{path} holds {ambiguous} ambiguous slash dates, but no order is "
            f"settled for {view.dataset_id}"
        )
    order = day_first[view.dataset_id]
    if witnesses[not order]:
        raise ValueError(
            f"{view.dataset_id} is recorded as "
            f"{'day' if order else 'month'} first, but {witnesses[not order]} of "
            "its values prove the other order"
        )
    if dry_run:
        return ambiguous
    reaction_ids = list(view.iter_reaction_ids())
    rewritten = 0
    with parquet.DatasetWriter(
        path,
        name=view.name,
        description=view.description,
        dataset_id=view.dataset_id,
    ) as writer:
        for reaction in view.reactions:
            changes = rewrite_reaction(reaction, day_first=order)
            if changes:
                modified = reaction.provenance.record_modified.add()
                modified.CopyFrom(event)
                modified.details = describe(changes)
                rewritten += len(changes)
            writer.write(reaction)
    written = parquet.DatasetView(path)
    if (
        (written.name, written.description, written.dataset_id)
        != (view.name, view.description, view.dataset_id)
        or list(written.iter_reaction_ids()) != reaction_ids
        or rewritten != ambiguous
        or scan(written)[0]
    ):
        raise RuntimeError(f"{path} does not read back as planned")
    return rewritten


def main() -> None:
    """Rewrite the ambiguous slash dates in every dataset under data/."""
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
    total = 0
    for path in paths:
        rewritten = rewrite_dataset(path, event, dry_run=args.dry_run)
        if rewritten:
            logger.info(f"{path.relative_to(args.repo_root)}  {rewritten}")
            total += rewritten
    verb = "would rewrite" if args.dry_run else "rewrote"
    logger.info(f"{verb} {total} values across {len(paths)} datasets")


if __name__ == "__main__":
    main()
