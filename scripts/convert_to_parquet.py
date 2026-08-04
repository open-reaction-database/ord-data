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

"""Convert ord-data Dataset protos (.pb.gz) to Parquet.

Each dataset is converted 1:1, carrying forward name/description/dataset_id.
Outputs are placed at ``data/<2-hex-prefix>/ord_dataset-<id>.parquet`` where
the prefix matches the dataset_id. After converting, delete the ``.pb.gz``
inputs so only the Parquet versions are committed (see README.md).

Re-running is safe: an output that already holds the same reaction count as its
input is left alone. An output that disagrees, or two inputs claiming one output
path, is an error rather than a skip -- the documented procedure deletes the
``.pb.gz`` afterwards, so a wrongly skipped file would lose its only copy.
"""

import argparse
import logging
import re
import sys
from pathlib import Path

from ord_schema import message_helpers, parquet
from ord_schema.proto import dataset_pb2

logger = logging.getLogger(__name__)

INPUT_GLOB = "data/*/ord_dataset-*.pb.gz"
DATASET_ID_PATTERN = re.compile(r"ord_dataset-[0-9a-f]{32}$")


def _output_path(repo_root: Path, dataset_id: str) -> Path:
    prefix = dataset_id[len("ord_dataset-") :][:2]
    return repo_root / "data" / prefix / f"{dataset_id}.parquet"


def _convert(
    src: Path, repo_root: Path, dry_run: bool, claimed: dict[Path, Path]
) -> str:
    """Converts one pb.gz dataset to Parquet, or reports why it was skipped.

    Args:
        src: Path to the ``.pb.gz`` input.
        repo_root: Repository root containing ``data/``.
        dry_run: If True, plan only and write nothing.
        claimed: Output paths already claimed this run, mapped to the input that
            claimed them. Updated in place.

    Returns:
        A one-line log message describing what happened.

    Raises:
        ValueError: If the dataset_id is missing or malformed, if another input
            already claimed this output path, or if an existing output disagrees
            with the input about the reaction count.
    """
    dataset = message_helpers.load_message(str(src), dataset_pb2.Dataset)
    # The prefix directory is sliced straight out of the id, so a malformed id
    # would silently place the file outside data/<xx>/ where validation.yml
    # looks for it.
    if not DATASET_ID_PATTERN.fullmatch(dataset.dataset_id):
        raise ValueError(f"malformed dataset_id {dataset.dataset_id!r}")
    out = _output_path(repo_root, dataset.dataset_id)
    rel = out.relative_to(repo_root)
    if (prior := claimed.get(out)) is not None:
        raise ValueError(
            f"dataset_id {dataset.dataset_id} was already written from "
            f"{prior.relative_to(repo_root)}; resolve the duplicate id first"
        )
    claimed[out] = src
    if out.exists():
        existing = len(parquet.DatasetView(str(out)).reactions)
        if existing != len(dataset.reactions):
            raise ValueError(
                f"{rel} exists with {existing} reactions but this input has "
                f"{len(dataset.reactions)}; delete the stale output to reconvert"
            )
        return f"skip (verified) {rel}  ({existing} rxns)"
    if dry_run:
        return f"would write     {rel}  ({len(dataset.reactions)} rxns)"
    out.parent.mkdir(parents=True, exist_ok=True)
    parquet.save_dataset(dataset, str(out))
    return f"wrote           {rel}  ({len(dataset.reactions)} rxns)"


def main() -> None:
    """Converts each pb.gz dataset under data/ to Parquet, one output per input."""
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
        help="Plan only; do not write any parquet files.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Distinguish "aimed at the wrong tree" from "nothing to convert": with the
    # corpus stored as Parquet only, an empty glob is the expected steady state
    # and must not look like a failure to whatever runs this.
    if not (args.repo_root / "data").is_dir():
        sys.exit(f"No data/ directory under {args.repo_root}; pass --repo-root.")
    inputs = sorted(args.repo_root.glob(INPUT_GLOB))
    if not inputs:
        logger.info("Nothing to convert: no %s under %s.", INPUT_GLOB, args.repo_root)
        return
    logger.info("Found %d pb.gz inputs", len(inputs))

    # Report every bad input, not just the first, matching how the submission
    # pipeline treats a batch: one run should surface the whole problem.
    claimed: dict[Path, Path] = {}
    failures: list[str] = []
    for src in inputs:
        try:
            logger.info(_convert(src, args.repo_root, args.dry_run, claimed))
        except (ValueError, OSError) as error:
            rel = src.relative_to(args.repo_root)
            logger.info("FAILED          %s", rel)
            failures.append(f"{rel}: {error}")
    if failures:
        detail = "\n".join(f"  {failure}" for failure in failures)
        sys.exit(
            f"{len(failures)} of {len(inputs)} inputs failed to convert:\n{detail}"
        )


if __name__ == "__main__":
    main()
