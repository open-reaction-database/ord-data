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

"""Mirror ord-data changes to the Hugging Face dataset.

Runs `git diff --name-status` between two SHAs, classifies entries into
uploads and deletions, fetches only the needed LFS objects, and applies
the changes as a single commit on the Hugging Face dataset at
https://huggingface.co/datasets/open-reaction-database/ord-data.

The mirror's README is a composed dataset card — YAML front matter (license,
tags, citation, and one config per dataset) followed by the GitHub README
body — so the Hugging Face page has a working dataset viewer and searchable
metadata. The GitHub README itself stays plain (front matter would render as
a table there). The card is regenerated on every mirror commit, so its config
list always matches the parquet files on disk.

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

import yaml
from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

HF_REPO_ID = "open-reaction-database/ord-data"
# Files mirrored to the HF dataset. Datasets + dataset-card-relevant docs +
# .gitattributes (so LFS rules stay in sync). GitHub-side infrastructure
# (.github/, scripts/, badges/) is intentionally excluded.
MIRROR_PATHSPECS = (
    "data/**",
    ".gitattributes",
    "README.md",
    "LICENSE",
    "CITATION.cff",
    "CONTRIBUTING.md",
    "CONTRIBUTORS.md",
)

# Dataset-card front matter written only to the Hugging Face mirror.
CARD_LICENSE = "cc-by-sa-4.0"
CARD_TAGS = ("chemistry", "reactions", "cheminformatics")
CARD_PRETTY_NAME = "The Open Reaction Database"

# Datasets that get a named config in addition to their `ord_dataset-<id>` one.
# `uspto-grants` is the full patent extraction; `uspto-mit` is the reaction
# prediction benchmark (10.1039/C8SC04228D) with real train/validation/test
# splits. The default config is every dataset except these. Keyed by
# dataset_id (the parquet filename stem), which is stable across renames.
USPTO_GRANTS_ID = "ord_dataset-1158e351757f315b93cbcbe7bc55f38e"
USPTO_MIT_SPLITS = {
    "train": "ord_dataset-e7830cd6b11158b43994ccfb5ee9acb3",
    "validation": "ord_dataset-5481550056a14935b76e031fb94b88be",
    "test": "ord_dataset-488402f6ec0d441ca2f7d6fabea7c220",
}


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
            "--diff-filter=ACMRD", base, head, "--", *MIRROR_PATHSPECS,
        ],
        cwd=repo_root, check=True, capture_output=True, text=True,
    )
    return parse_name_status(diff.stdout)


def write_summary(
    uploads: list[str],
    deletions: list[str],
    composed_readme: str | None,
    path: Path | None,
) -> None:
    if path is None:
        return
    lines = [
        f"## Hugging Face mirror plan ({HF_REPO_ID})",
        "",
        f"- Uploads: **{len(uploads)}**",
        f"- Deletions: **{len(deletions)}**",
    ]
    if uploads:
        lines += ["", "### Uploads", "", "```", *uploads, "```"]
    if deletions:
        lines += ["", "### Deletions", "", "```", *deletions, "```"]
    if composed_readme is not None:
        # Fence with four backticks so the README's own ``` blocks survive.
        lines += [
            "",
            "### Composed dataset card (`README.md` on Hugging Face)",
            "",
            "<details><summary>Show composed README.md</summary>",
            "",
            "````markdown",
            composed_readme.rstrip(),
            "````",
            "",
            "</details>",
        ]
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


def _dataset_id(parquet_path: str) -> str:
    """Return the dataset_id (parquet filename stem, e.g. ``ord_dataset-<hex>``)."""
    return Path(parquet_path).stem


def build_configs(repo_root: Path) -> list[dict]:
    """Build the Hugging Face ``configs`` list from the parquet files on disk.

    Emits a ``default`` config over every dataset except the two large USPTO
    artifacts, named ``uspto-grants`` and ``uspto-mit`` configs, and one
    ``ord_dataset-<id>`` config per dataset. Only filenames are read, so no LFS
    objects need to be present.

    Args:
        repo_root: Repository root containing the ``data/`` tree.

    Returns:
        A list of Hugging Face config dicts, ``default`` first.
    """
    paths = sorted(
        p.relative_to(repo_root).as_posix()
        for p in repo_root.glob("data/*/*.parquet")
    )
    by_id = {_dataset_id(p): p for p in paths}
    named_ids = {USPTO_GRANTS_ID, *USPTO_MIT_SPLITS.values()}

    configs: list[dict] = [
        {
            "config_name": "default",
            "default": True,
            "data_files": [p for p in paths if _dataset_id(p) not in named_ids],
        }
    ]
    if USPTO_GRANTS_ID in by_id:
        configs.append(
            {"config_name": "uspto-grants", "data_files": [by_id[USPTO_GRANTS_ID]]}
        )
    if set(USPTO_MIT_SPLITS.values()).issubset(by_id):
        configs.append(
            {
                "config_name": "uspto-mit",
                "data_files": [
                    {"split": split, "path": by_id[dataset_id]}
                    for split, dataset_id in USPTO_MIT_SPLITS.items()
                ],
            }
        )
    configs += [{"config_name": _dataset_id(p), "data_files": [p]} for p in paths]
    return configs


def build_citation(repo_root: Path) -> str:
    """Format a BibTeX entry from ``CITATION.cff``'s ``preferred-citation``.

    Args:
        repo_root: Repository root containing ``CITATION.cff``.

    Returns:
        A BibTeX ``@article`` entry as a string.
    """
    data = yaml.safe_load((repo_root / "CITATION.cff").read_text())
    ref = data.get("preferred-citation", data)
    authors = ref.get("authors", [])
    author_field = " and ".join(
        f"{a['family-names']}, {a['given-names']}" if "family-names" in a else a["name"]
        for a in authors
    )
    fields = {
        "author": author_field or None,
        "title": ref.get("title"),
        "journal": ref.get("journal"),
        "year": ref.get("year"),
        "volume": ref.get("volume"),
        "number": ref.get("issue"),
        "pages": (
            f"{ref['start']}--{ref['end']}"
            if ref.get("start") and ref.get("end")
            else None
        ),
        "doi": ref.get("doi"),
    }
    surname = authors[0].get("family-names", "ord") if authors else "ord"
    key = f"{surname.split()[0].lower()}{ref.get('year', '')}ord"
    lines = [f"@article{{{key},"]
    lines += [f"  {name} = {{{value}}}," for name, value in fields.items() if value]
    lines.append("}")
    return "\n".join(lines)


def compose_readme(repo_root: Path) -> str:
    """Compose the mirror README: card front matter, GitHub body, and citation.

    Args:
        repo_root: Repository root containing ``README.md`` and the dataset.

    Returns:
        The full Markdown text to upload to Hugging Face as ``README.md``.
    """
    metadata = {
        "license": CARD_LICENSE,
        "tags": list(CARD_TAGS),
        "pretty_name": CARD_PRETTY_NAME,
        "configs": build_configs(repo_root),
    }
    front_matter = yaml.safe_dump(
        metadata, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    body = (repo_root / "README.md").read_text().rstrip()
    citation = build_citation(repo_root)
    return (
        f"---\n{front_matter}---\n\n"
        f"{body}\n\n"
        "## Citation\n\n"
        "If you use this dataset, please cite the Open Reaction Database paper:\n\n"
        f"```bibtex\n{citation}\n```\n"
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
    # README is a composed card, uploaded from memory rather than mirrored
    # verbatim. Track whether its GitHub body changed before dropping it, since
    # a body-only edit still needs to refresh the card on Hugging Face.
    readme_changed = "README.md" in plan.uploads
    plan.uploads = [u for u in plan.uploads if u != "README.md"]
    has_changes = bool(plan.uploads or plan.deletions or readme_changed)
    # The config list is derived from all parquet on disk, so any add/delete
    # can change it; recompose the card whenever there is anything to mirror.
    composed_readme = compose_readme(args.repo_root) if has_changes else None

    uploads = list(plan.uploads)
    if composed_readme is not None:
        uploads.append("README.md (composed dataset card)")

    print(f"Planned uploads ({len(uploads)}):")
    for p in uploads:
        print(f"  + {p}")
    print(f"Planned deletions ({len(plan.deletions)}):")
    for p in plan.deletions:
        print(f"  - {p}")
    write_summary(uploads, plan.deletions, composed_readme, args.summary_file)

    if not has_changes:
        print("Nothing to mirror.")
        return
    if args.dry_run:
        if composed_readme is not None:
            _, front_matter, _ = composed_readme.split("---\n", 2)
            print("\nComposed dataset card front matter (license, tags, configs):")
            print(front_matter.rstrip())
        print("\nDry run: not fetching LFS or contacting Hugging Face.")
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
    operations.append(
        CommitOperationAdd(
            path_in_repo="README.md", path_or_fileobj=composed_readme.encode()
        )
    )

    HfApi(token=token).create_commit(
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        operations=operations,
        commit_message=args.commit_message,
    )
    print(
        f"Mirrored {len(plan.uploads) + 1} upload(s) (incl. composed README.md) "
        f"and {len(plan.deletions)} deletion(s) to {HF_REPO_ID}."
    )


if __name__ == "__main__":
    main()
