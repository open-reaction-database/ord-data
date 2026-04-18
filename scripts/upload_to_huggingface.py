"""Upload ord-data dataset files to the Hugging Face mirror.

Reads a newline-delimited list of repo-relative paths from a file (one
per line) and uploads them as a single commit to the Hugging Face
dataset at https://huggingface.co/datasets/open-reaction-database/ord-data.

Authentication uses the HF_TOKEN environment variable.

This script is invoked from `.github/workflows/huggingface_mirror.yml`;
it can also be run locally for manual backfills.
"""

import argparse
import os
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi

HF_REPO_ID = "open-reaction-database/ord-data"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--files-from",
        type=Path,
        required=True,
        help="File containing newline-delimited repo-relative paths to upload.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Local repository root (default: parent of scripts/).",
    )
    parser.add_argument(
        "--commit-message",
        default="Mirror update from GitHub",
        help="Commit message for the Hugging Face commit.",
    )
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN environment variable is not set.")

    paths = [
        line.strip()
        for line in args.files_from.read_text().splitlines()
        if line.strip()
    ]
    if not paths:
        print("No files to upload.")
        return

    operations = []
    for path in paths:
        local_path = args.repo_root / path
        if not local_path.exists():
            # File was deleted in this commit range; skip (deletions are
            # not mirrored automatically — handle those manually if needed).
            print(f"Skipping missing file: {path}")
            continue
        operations.append(
            CommitOperationAdd(path_in_repo=path, path_or_fileobj=str(local_path))
        )

    if not operations:
        print("No existing files to upload.")
        return

    api = HfApi(token=token)
    api.create_commit(
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        operations=operations,
        commit_message=args.commit_message,
    )
    print(f"Uploaded {len(operations)} file(s) to {HF_REPO_ID}.")


if __name__ == "__main__":
    main()
