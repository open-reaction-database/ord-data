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

"""Split the consolidated uspto-grants dataset into one dataset per grant year.

Reactions do not record when their patent was granted, so the year comes from
the retired monthly ``uspto-grants-YYYY_MM`` datasets that ``uspto-grants``
consolidated. They are read out of Git LFS at ``--revision``, the last commit
that held them, and contribute only reaction IDs and their order; reaction
contents are copied from the consolidated Parquet file.

Each year is written to ``data/<xx>/ord_dataset-<id>.parquet`` with its months
in calendar order and each month in its original order. A year's dataset_id is
derived from the sorted IDs of its monthly sources, the rule README.md gives for
consolidated datasets, so re-runs write the same files.
"""

import argparse
import calendar
import collections
import csv
import gzip
import hashlib
import logging
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ord_schema import parquet
from ord_schema.proto import dataset_pb2

logger = logging.getLogger(__name__)

SOURCE_ID = "ord_dataset-1158e351757f315b93cbcbe7bc55f38e"
# Parent of the commit that deleted the monthly .pb.gz datasets (#284).
REVISION = "05fe1a62cb36d6bc3c07592ac1bd4ff1a4d8e43c"
MONTHLY_NAME = re.compile(r"uspto-grants-(\d{4})_(0[1-9]|1[0-2])")


@dataclass(frozen=True)
class MonthlySource:
    """The reaction IDs of one retired monthly dataset, in their original order."""

    dataset_id: str
    year: int
    month: int
    reaction_ids: tuple[str, ...]


@dataclass(frozen=True)
class YearPlan:
    """One output dataset: a grant year and the monthly sources it gathers."""

    year: int
    dataset_id: str
    name: str
    description: str
    months: tuple[MonthlySource, ...]

    @property
    def reaction_ids(self) -> list[str]:
        """Reaction IDs by month in calendar order, each month in its source order."""
        return [rid for month in self.months for rid in month.reaction_ids]


def parse_month(name: str) -> tuple[int, int]:
    """Parse the grant year and month out of a monthly dataset name.

    Args:
        name: Dataset name such as ``uspto-grants-1976_01``.

    Returns:
        ``(year, month)``.

    Raises:
        ValueError: If ``name`` is not a monthly uspto-grants name.
    """
    match = MONTHLY_NAME.fullmatch(name)
    if match is None:
        raise ValueError(f"{name!r} is not a monthly uspto-grants name")
    return int(match[1]), int(match[2])


def derive_dataset_id(source_ids: Iterable[str]) -> str:
    """Derive a consolidated dataset's ID from the sorted IDs of its sources.

    Args:
        source_ids: dataset_ids of the datasets being consolidated.

    Returns:
        An ``ord_dataset-<32 hex>`` ID that depends only on the set of sources.
    """
    digest = hashlib.sha256(",".join(sorted(source_ids)).encode()).hexdigest()
    return f"ord_dataset-{digest[:32]}"


def describe_year(year: int, months: Sequence[int]) -> str:
    """Write the description for one year's dataset.

    Args:
        year: Grant year.
        months: Months of ``year`` the dataset covers.

    Returns:
        The dataset description, naming the months when they are not all twelve.

    Raises:
        ValueError: If ``months`` is not a contiguous run.
    """
    months = sorted(months)
    if months != list(range(months[0], months[-1] + 1)):
        raise ValueError(f"months {months} of {year} are not a contiguous run")
    first, last = calendar.month_name[months[0]], calendar.month_name[months[-1]]
    if len(months) == 12:
        period = f"in {year}"
    elif len(months) == 1:
        period = f"in {first} {year}"
    else:
        period = f"from {first} through {last} {year}"
    return (
        f"Reactions extracted from USPTO patents granted {period}. The "
        "uspto-grants data is published as one dataset per grant year; "
        "per-reaction patent provenance is on Reaction.provenance.patent."
    )


def plan_years(sources: Iterable[MonthlySource]) -> list[YearPlan]:
    """Group monthly sources into one output dataset per year.

    Args:
        sources: Monthly datasets, in any order.

    Returns:
        One plan per year, in year order, with months in calendar order.

    Raises:
        ValueError: If two sources claim the same month.
    """
    by_year: dict[int, dict[int, MonthlySource]] = collections.defaultdict(dict)
    for source in sources:
        months = by_year[source.year]
        if source.month in months:
            raise ValueError(
                f"{source.year}-{source.month:02d} appears in more than one "
                "monthly dataset"
            )
        months[source.month] = source
    plans = []
    for year, months in sorted(by_year.items()):
        ordered = tuple(months[month] for month in sorted(months))
        plans.append(
            YearPlan(
                year=year,
                dataset_id=derive_dataset_id(m.dataset_id for m in ordered),
                name=f"uspto-grants-{year}",
                description=describe_year(year, [m.month for m in ordered]),
                months=ordered,
            )
        )
    return plans


def output_path(repo_root: Path, dataset_id: str) -> Path:
    """Return where a dataset lives under ``data/``, keyed by its ID prefix."""
    prefix = dataset_id.removeprefix("ord_dataset-")[:2]
    return repo_root / "data" / prefix / f"{dataset_id}.parquet"


def check_partition(
    view: parquet.DatasetView, plans: Sequence[YearPlan]
) -> dict[str, int]:
    """Check that the plans place every source reaction exactly once.

    Args:
        view: The consolidated dataset.
        plans: Output datasets to be written from it.

    Returns:
        A map from reaction_id to its row position in ``view``.

    Raises:
        ValueError: If a reaction is unplaced, placed twice, or absent from the
            source, or if the source repeats a reaction_id.
    """
    source_ids = list(view.iter_reaction_ids())
    positions = {reaction_id: i for i, reaction_id in enumerate(source_ids)}
    if len(positions) != len(source_ids):
        raise ValueError(f"{view.path} repeats a reaction_id")
    placed = collections.Counter(rid for plan in plans for rid in plan.reaction_ids)
    if repeated := sorted(rid for rid, count in placed.items() if count > 1):
        raise ValueError(
            f"{len(repeated)} reactions appear in more than one month, "
            f"e.g. {repeated[0]}"
        )
    if missing := sorted(placed.keys() - positions.keys()):
        raise ValueError(
            f"{len(missing)} monthly reactions are not in the source, e.g. {missing[0]}"
        )
    if unplaced := sorted(positions.keys() - placed.keys()):
        raise ValueError(
            f"{len(unplaced)} source reactions have no month, e.g. {unplaced[0]}"
        )
    return positions


def write_years(
    view: parquet.DatasetView, plans: Sequence[YearPlan], repo_root: Path
) -> list[Path]:
    """Write one Parquet dataset per plan, copying reactions from ``view``.

    Nothing is written unless the plans place every source reaction exactly once.

    Args:
        view: The consolidated dataset.
        plans: Output datasets, from ``plan_years``.
        repo_root: Repository root containing ``data/``.

    Returns:
        The written paths, in plan order.

    Raises:
        ValueError: If the plans do not partition the source (see
            ``check_partition``).
        RuntimeError: If a written file does not read back as planned. That file
            is removed; files written before it are kept.
    """
    positions = check_partition(view, plans)
    reactions = view.reactions
    paths = []
    for plan in plans:
        path = output_path(repo_root, plan.dataset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with parquet.DatasetWriter(
            path,
            name=plan.name,
            description=plan.description,
            dataset_id=plan.dataset_id,
        ) as writer:
            for reaction_id in plan.reaction_ids:
                writer.write(reactions[positions[reaction_id]])
        written = parquet.DatasetView(path)
        if (
            written.dataset_id != plan.dataset_id
            or list(written.iter_reaction_ids()) != plan.reaction_ids
        ):
            path.unlink()
            raise RuntimeError(f"{path} does not read back as planned; removed it")
        logger.info(f"wrote {path.relative_to(repo_root)}  ({len(written.reactions)})")
        paths.append(path)
    return paths


def _read_lfs_dataset(repo_root: Path, revision: str, path: str) -> dataset_pb2.Dataset:
    """Read a ``.pb.gz`` Dataset that Git LFS stores at ``revision:path``."""
    pointer = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout
    content = subprocess.run(
        ["git", "lfs", "smudge", path],
        cwd=repo_root,
        input=pointer,
        check=True,
        capture_output=True,
    ).stdout
    return dataset_pb2.Dataset.FromString(gzip.decompress(content))


def read_monthly_sources(repo_root: Path, revision: str) -> list[MonthlySource]:
    """Read the reaction IDs of every retired monthly uspto-grants dataset.

    The monthly datasets are the ``retired_datasets.csv`` rows whose retired name
    matches ``uspto-grants-YYYY_MM``.

    Args:
        repo_root: Repository root containing ``retired_datasets.csv``.
        revision: A commit that still holds the monthly ``.pb.gz`` files.

    Returns:
        One source per monthly dataset, in ``retired_datasets.csv`` order.

    Raises:
        ValueError: If a file's name or dataset_id disagrees with its CSV row.
    """
    with (repo_root / "retired_datasets.csv").open(newline="") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if MONTHLY_NAME.fullmatch(row["retired_name"])
        ]
    sources = []
    for row in rows:
        dataset_id, name = row["retired_dataset_id"], row["retired_name"]
        path = output_path(Path(), dataset_id).with_suffix(".pb.gz").as_posix()
        dataset = _read_lfs_dataset(repo_root, revision, path)
        if (dataset.dataset_id, dataset.name) != (dataset_id, name):
            raise ValueError(
                f"{revision}:{path} holds {dataset.name!r} ({dataset.dataset_id}), "
                f"but retired_datasets.csv lists {name!r} ({dataset_id})"
            )
        year, month = parse_month(name)
        sources.append(
            MonthlySource(
                dataset_id=dataset_id,
                year=year,
                month=month,
                reaction_ids=tuple(r.reaction_id for r in dataset.reactions),
            )
        )
    return sources


def main() -> None:
    """Split the consolidated uspto-grants dataset into per-year datasets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root containing data/ (default: parent of this script's dir).",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help=f"Consolidated Parquet file (default: {SOURCE_ID} under data/).",
    )
    parser.add_argument(
        "--revision",
        default=REVISION,
        help="Commit holding the monthly .pb.gz datasets.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan and check the split; do not write any files.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sources = read_monthly_sources(args.repo_root, args.revision)
    logger.info(f"read {len(sources)} monthly datasets at {args.revision}")
    plans = plan_years(sources)
    for plan in plans:
        logger.info(
            f"{plan.name}  {plan.dataset_id}  {len(plan.months)} months  "
            f"{len(plan.reaction_ids)} reactions"
        )
    view = parquet.DatasetView(args.source or output_path(args.repo_root, SOURCE_ID))
    if args.dry_run:
        check_partition(view, plans)
        logger.info(f"{len(plans)} years place all {len(view.reactions)} reactions")
        return
    paths = write_years(view, plans, args.repo_root)
    logger.info(f"wrote {len(paths)} datasets from {len(view.reactions)} reactions")


if __name__ == "__main__":
    main()
