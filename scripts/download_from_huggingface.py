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

"""Download ord-data dataset files from Hugging Face.

Use this as an alternative to `git lfs pull` when Git LFS bandwidth is
unavailable or exhausted. The script mirrors the repository's `data/`
directory from the Hugging Face dataset at
https://huggingface.co/datasets/open-reaction-database/ord-data.

Usage:
    pip install huggingface_hub
    python scripts/download_from_huggingface.py

Optional flags let you restrict the download to a subset of files or
target a different local directory.
"""

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download

HF_REPO_ID = "open-reaction-database/ord-data"
DEFAULT_ALLOW_PATTERNS = ["data/**"]


def main() -> None:
    """Downloads the data/ tree from the Hugging Face mirror."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Local directory to mirror the dataset into (default: repo root).",
    )
    parser.add_argument(
        "--revision",
        default="main",
        help="Branch, tag, or commit SHA to download (default: main).",
    )
    parser.add_argument(
        "--allow-pattern",
        action="append",
        default=None,
        help=(
            "Glob pattern(s) of files to include (repeatable). "
            f"Default: {DEFAULT_ALLOW_PATTERNS}. "
            "Example: --allow-pattern 'data/4d/*.parquet'"
        ),
    )
    args = parser.parse_args()

    allow_patterns = args.allow_pattern or DEFAULT_ALLOW_PATTERNS
    args.output_dir.mkdir(parents=True, exist_ok=True)
    local_dir = snapshot_download(
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        revision=args.revision,
        local_dir=str(args.output_dir),
        allow_patterns=allow_patterns,
    )
    print(f"Downloaded {HF_REPO_ID}@{args.revision} to {local_dir}")


if __name__ == "__main__":
    main()
