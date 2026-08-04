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
the prefix matches the dataset_id. Existing outputs are skipped, so the script
is safe to re-run. After converting, delete the ``.pb.gz`` inputs so only the
Parquet versions are committed (see README.md).
"""

import argparse
import logging
import sys
from pathlib import Path

from ord_schema import message_helpers, parquet
from ord_schema.proto import dataset_pb2

logger = logging.getLogger(__name__)

INPUT_GLOB = "data/*/ord_dataset-*.pb.gz"


def _output_path(repo_root: Path, dataset_id: str) -> Path:
    prefix = dataset_id[len("ord_dataset-") :][:2]
    return repo_root / "data" / prefix / f"{dataset_id}.parquet"


def _convert(src: Path, repo_root: Path, dry_run: bool) -> str:
    dataset = message_helpers.load_message(str(src), dataset_pb2.Dataset)
    if not dataset.dataset_id:
        raise ValueError(f"{src}: missing dataset_id")
    out = _output_path(repo_root, dataset.dataset_id)
    rel = out.relative_to(repo_root)
    if out.exists():
        return f"skip (exists)  {rel}"
    if dry_run:
        return f"would write    {rel}  ({len(dataset.reactions)} rxns)"
    out.parent.mkdir(parents=True, exist_ok=True)
    parquet.save_dataset(dataset, str(out))
    return f"wrote          {rel}  ({len(dataset.reactions)} rxns)"


def main() -> None:
    """Converts pb.gz datasets to Parquet siblings, one output per input."""
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

    inputs = sorted(args.repo_root.glob(INPUT_GLOB))
    if not inputs:
        sys.exit(f"No inputs matched {args.repo_root / INPUT_GLOB}")
    logger.info("Found %d pb.gz inputs", len(inputs))

    for src in inputs:
        logger.info(_convert(src, args.repo_root, args.dry_run))


if __name__ == "__main__":
    main()
