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
    "retired_datasets.csv",
)

# Dataset-card front matter written only to the Hugging Face mirror.
CARD_LICENSE = "cc-by-sa-4.0"
CARD_TAGS = ("chemistry", "reactions", "cheminformatics")
CARD_PRETTY_NAME = "The Open Reaction Database"

# Datasets that get a named config in addition to their `ord_dataset-<id>` one.
# `uspto-grants` is the full patent extraction, one dataset per grant year;
# `uspto-mit` is the reaction prediction benchmark (10.1039/C8SC04228D) with
# real train/validation/test splits. The default config is every dataset except
# these. Keyed by dataset_id (the parquet filename stem), which is stable across
# renames.
USPTO_GRANTS_IDS = {
    1976: "ord_dataset-ca811a23e9d44f4f0a65d6456701856e",
    1977: "ord_dataset-a7599c88da61302505d456077341c429",
    1978: "ord_dataset-916293e0c239ca8463ad7468f8c46236",
    1979: "ord_dataset-2644e8b8f6987d8aabf57f04dab558dd",
    1980: "ord_dataset-a708306f1cb43cc6ef1301dfb9018191",
    1981: "ord_dataset-f2718cd03ad7dd73fef23ea3c79f2c95",
    1982: "ord_dataset-fd38f429352a645714f44afdbc6807b5",
    1983: "ord_dataset-19ba360ec1bd15a2f73a937a1d161e4f",
    1984: "ord_dataset-2ac92882468f4d47ec7ca0eda9b31cd6",
    1985: "ord_dataset-fb9c9799c8535f36029173e90ba97497",
    1986: "ord_dataset-0078ecb24abd66512594995d2e098f5e",
    1987: "ord_dataset-bd77aaca65a9d5ef4cbc3f2808115f28",
    1988: "ord_dataset-166e12c0a91ded488db9e3129c1ad034",
    1989: "ord_dataset-f82e41201e406c71af831481565bcfb6",
    1990: "ord_dataset-7006210ae79ad5ca553ae4f011390193",
    1991: "ord_dataset-557fc289692260cdbb1f6fd86ff221eb",
    1992: "ord_dataset-4b5063eddeab92294a10ead7a637f15d",
    1993: "ord_dataset-e4994f55eb1de3d4e9f35f0c5160f213",
    1994: "ord_dataset-493459620170b5cfa2f6a0ff4c6f6c3e",
    1995: "ord_dataset-5c7e67eda2a18cecb036e009e11c60be",
    1996: "ord_dataset-8709a7a6729268b0ea8055fd8ab6ec2c",
    1997: "ord_dataset-ed85599810a968b103e70c8b6f0aa3b0",
    1998: "ord_dataset-5efc247455d702206edf2e3e9a7d611e",
    1999: "ord_dataset-758207d0e333f96f6342bad3dfddf1e2",
    2000: "ord_dataset-0fbb04349518b2a00ff291c9ac3263e9",
    2001: "ord_dataset-bd016cde31e4e78205ecec8170787fd7",
    2002: "ord_dataset-658fd3543b0e967af40b7dc959a0d07b",
    2003: "ord_dataset-286194ee34e80404838b65248be49461",
    2004: "ord_dataset-52eb859eec1d012e947a2cf560024948",
    2005: "ord_dataset-9477c7f5dc176dcc5657fe7df9476027",
    2006: "ord_dataset-92c79d38c8cb08545d77306934156875",
    2007: "ord_dataset-30596ddbc19b4f46fb58751d65eb1982",
    2008: "ord_dataset-9711b36895ef05e2fb77f0b439c5f985",
    2009: "ord_dataset-0c0a93e5eb286a8fa697b1fdd8231f85",
    2010: "ord_dataset-90d3da23f90d8b8e992251ddce471e0a",
    2011: "ord_dataset-8de1755b921e4726dc5a2851519cf398",
    2012: "ord_dataset-f89bba9745e12c2116a5b6be9b2873ad",
    2013: "ord_dataset-d68bd72dc7a8c7c3db14a93e4d8f0d30",
    2014: "ord_dataset-3d996df040a270112aabe120c3727a51",
    2015: "ord_dataset-1254900636d6635dfbfb46101295b2e2",
    2016: "ord_dataset-8739070cee44634940b6d9717333a5bb",
}
USPTO_MIT_SPLITS = {
    "train": "ord_dataset-e7830cd6b11158b43994ccfb5ee9acb3",
    "validation": "ord_dataset-5481550056a14935b76e031fb94b88be",
    "test": "ord_dataset-488402f6ec0d441ca2f7d6fabea7c220",
}


@dataclass
class DiffPlan:
    """Paths to upload to and delete from the mirror."""

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
    """Returns the upload/delete plan for mirrored paths between two commits."""
    diff = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames",
            "--diff-filter=ACMRD",
            base,
            head,
            "--",
            *MIRROR_PATHSPECS,
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_name_status(diff.stdout)


def write_summary(
    uploads: list[str],
    deletions: list[str],
    composed_readme: str | None,
    path: Path | None,
) -> None:
    """Writes a Markdown summary of the plan to ``path``; a no-op when unset."""
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
    """Materializes the LFS objects for ``paths``; a no-op when empty."""
    if not paths:
        return
    subprocess.run(
        ["git", "lfs", "pull", "--include", ",".join(paths)],
        cwd=repo_root,
        check=True,
    )


def _dataset_id(parquet_path: str) -> str:
    """Return the dataset_id (parquet filename stem, e.g. ``ord_dataset-<hex>``)."""
    return Path(parquet_path).stem


def build_configs(repo_root: Path) -> list[dict]:
    """Build the Hugging Face ``configs`` list from the parquet files on disk.

    Emits a ``default`` config over every dataset except the large USPTO
    artifacts, named ``uspto-grants`` (its years in order) and ``uspto-mit``
    configs, and one ``ord_dataset-<id>`` config per dataset. Only filenames are
    read, so no LFS objects need to be present.

    Args:
        repo_root: Repository root containing the ``data/`` tree.

    Returns:
        A list of Hugging Face config dicts, ``default`` first.
    """
    paths = sorted(
        p.relative_to(repo_root).as_posix() for p in repo_root.glob("data/*/*.parquet")
    )
    by_id = {_dataset_id(p): p for p in paths}
    named_ids = {*USPTO_GRANTS_IDS.values(), *USPTO_MIT_SPLITS.values()}

    configs: list[dict] = [
        {
            "config_name": "default",
            "default": True,
            "data_files": [p for p in paths if _dataset_id(p) not in named_ids],
        }
    ]
    if set(USPTO_GRANTS_IDS.values()).issubset(by_id):
        configs.append(
            {
                "config_name": "uspto-grants",
                "data_files": [
                    by_id[dataset_id]
                    for _, dataset_id in sorted(USPTO_GRANTS_IDS.items())
                ],
            }
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
    """Mirrors the diff between two commits to the Hugging Face dataset."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Base git ref/SHA.")
    parser.add_argument("--head", required=True, help="Head git ref/SHA.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument(
        "--commit-message",
        default="Mirror update from GitHub",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
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
    # The config list is derived from all parquet on disk, so any add/delete
    # can change it; recompose the card whenever there is anything to mirror.
    # A composed card therefore doubles as "there is something to mirror".
    composed_readme = (
        compose_readme(args.repo_root)
        if (plan.uploads or plan.deletions or readme_changed)
        else None
    )

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

    if composed_readme is None:
        print("Nothing to mirror.")
        return
    if args.dry_run:
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
            raise SystemExit(
                f"Expected upload target {local_path} missing after LFS pull."
            )
        operations.append(
            CommitOperationAdd(path_in_repo=path, path_or_fileobj=str(local_path))
        )
    operations.extend(
        CommitOperationDelete(path_in_repo=path) for path in plan.deletions
    )
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
