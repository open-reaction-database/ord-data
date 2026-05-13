"""Mirror ord-data changes to the Hugging Face dataset.

Runs `git diff --name-status` between two SHAs, classifies entries into
uploads and deletions, fetches only the needed LFS objects, and applies
the changes as a single commit on the Hugging Face dataset at
https://huggingface.co/datasets/open-reaction-database/ord-data.

Authentication uses the HF_TOKEN environment variable (not required in
`--dry-run` mode).

This script is invoked from `.github/workflows/huggingface_mirror.yml`;
it can also be run locally for manual backfills.
"""

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

HF_REPO_ID = "open-reaction-database/ord-data"
DATA_PATHSPEC = "data/**"


@dataclass
class DiffPlan:
    uploads: list[str] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)


def parse_name_status(diff_text: str) -> DiffPlan:
    """Parse `git diff --name-status --find-renames` output.

    Added/Modified entries become uploads. Copies upload the destination.
    Deletions become deletions. Renames split into delete(old) + upload(new).
    """
    plan = DiffPlan()
    for raw in diff_text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0][0]
        if code in ("A", "M"):
            plan.uploads.append(parts[1])
        elif code == "D":
            plan.deletions.append(parts[1])
        elif code == "R":
            plan.deletions.append(parts[1])
            plan.uploads.append(parts[2])
        elif code == "C":
            plan.uploads.append(parts[2])
        else:
            print(f"Skipping unrecognized status line: {line!r}", file=sys.stderr)
    return plan


def compute_plan(base: str, head: str, repo_root: Path) -> DiffPlan:
    diff = subprocess.run(
        [
            "git", "diff", "--name-status", "--find-renames",
            "--diff-filter=ACMRD", base, head, "--", DATA_PATHSPEC,
        ],
        cwd=repo_root, check=True, capture_output=True, text=True,
    )
    return parse_name_status(diff.stdout)


def write_summary(plan: DiffPlan, path: Path | None) -> None:
    if path is None:
        return
    lines = [
        f"## Hugging Face mirror plan ({HF_REPO_ID})",
        "",
        f"- Uploads: **{len(plan.uploads)}**",
        f"- Deletions: **{len(plan.deletions)}**",
    ]
    if plan.uploads:
        lines += ["", "### Uploads", "", "```", *plan.uploads, "```"]
    if plan.deletions:
        lines += ["", "### Deletions", "", "```", *plan.deletions, "```"]
    # Append rather than overwrite so it plays well with $GITHUB_STEP_SUMMARY.
    with path.open("a") as fh:
        fh.write("\n".join(lines) + "\n")


def lfs_pull(paths: list[str], repo_root: Path) -> None:
    if not paths:
        return
    subprocess.run(
        ["git", "lfs", "pull", "--include", ",".join(paths)],
        cwd=repo_root, check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Base git ref/SHA.")
    parser.add_argument("--head", required=True, help="Head git ref/SHA.")
    parser.add_argument(
        "--repo-root", type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument(
        "--commit-message", default="Mirror update from GitHub",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and exit without fetching LFS or contacting HF.",
    )
    parser.add_argument("--summary-file", type=Path, default=None)
    args = parser.parse_args()

    plan = compute_plan(args.base, args.head, args.repo_root)

    print(f"Planned uploads ({len(plan.uploads)}):")
    for p in plan.uploads:
        print(f"  + {p}")
    print(f"Planned deletions ({len(plan.deletions)}):")
    for p in plan.deletions:
        print(f"  - {p}")
    write_summary(plan, args.summary_file)

    if not plan.uploads and not plan.deletions:
        print("Nothing to mirror.")
        return
    if args.dry_run:
        print("Dry run: not fetching LFS or contacting Hugging Face.")
        return

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN environment variable is not set.")

    lfs_pull(plan.uploads, args.repo_root)

    operations: list[CommitOperationAdd | CommitOperationDelete] = []
    for path in plan.uploads:
        local_path = args.repo_root / path
        if not local_path.exists():
            raise SystemExit(f"Expected upload target {local_path} missing after LFS pull.")
        operations.append(
            CommitOperationAdd(path_in_repo=path, path_or_fileobj=str(local_path))
        )
    for path in plan.deletions:
        operations.append(CommitOperationDelete(path_in_repo=path))

    HfApi(token=token).create_commit(
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        operations=operations,
        commit_message=args.commit_message,
    )
    print(
        f"Mirrored {len(plan.uploads)} upload(s) and "
        f"{len(plan.deletions)} deletion(s) to {HF_REPO_ID}."
    )


if __name__ == "__main__":
    main()
