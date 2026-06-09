"""Convert ord-data Dataset protos (.pb.gz) to Parquet, alongside the originals.

Most datasets are converted 1:1 (parquet sibling next to the pb.gz, carrying
forward name/description/dataset_id). A small set of explicitly-listed
multi-file groups are merged into single parquet outputs ("un-sharding"):

* All ``uspto-grants-YYYY_MM`` monthly buckets become one ``uspto-grants``
  parquet. Per-month CML filenames are dropped from the description; per-
  reaction patent provenance is preserved.
* The ten ``Training data from .../C8SC04228D (N/10)`` shards become one
  parquet with the (N/10) marker stripped from the name.

Each merged output gets a deterministic new ``dataset_id`` derived from the
SHA-256 of the sorted source ids, so re-runs produce the same output filename.

Outputs are placed at ``data/<2-hex-prefix>/ord_dataset-<id>.parquet`` where
the prefix matches the (existing or new) dataset_id. Existing outputs are
skipped, so the script is safe to re-run.
"""

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from ord_schema import message_helpers, parquet_dataset
from ord_schema.proto import dataset_pb2

logger = logging.getLogger(__name__)

INPUT_GLOB = "data/*/ord_dataset-*.pb.gz"


@dataclass(frozen=True)
class MergeSpec:
    """A group of pb.gz inputs to be merged into a single parquet output."""
    label: str  # human-readable for logs
    name: str  # output Dataset.name
    description: str  # output Dataset.description
    matches: callable  # (dataset_pb2.Dataset) -> bool


MERGE_SPECS: list[MergeSpec] = [
    MergeSpec(
        label="uspto-grants",
        name="uspto-grants",
        description=(
            "Reactions extracted from USPTO granted patents (1976-present), "
            "originally distributed as monthly buckets and consolidated here "
            "into a single dataset. Per-reaction patent provenance is "
            "available via Reaction.provenance.patent."
        ),
        matches=lambda ds: ds.name.startswith("uspto-grants-"),
    ),
    MergeSpec(
        label="C8SC04228D-training",
        name="Training data from https://doi.org/10.1039/C8SC04228D",
        description=(
            "409035 reaction SMILES downloaded from "
            "https://github.com/connorcoley/rexgen_direct"
        ),
        matches=lambda ds: ds.name.startswith(
            "Training data from https://doi.org/10.1039/C8SC04228D"
        ),
    ),
]


def _output_path(repo_root: Path, dataset_id: str) -> Path:
    prefix = dataset_id[len("ord_dataset-"):][:2]
    return repo_root / "data" / prefix / f"{dataset_id}.parquet"


def _derive_id(source_ids: list[str]) -> str:
    """Deterministic dataset_id for a merged output, derived from inputs."""
    digest = hashlib.sha256(",".join(sorted(source_ids)).encode()).hexdigest()[:32]
    return f"ord_dataset-{digest}"


def _load_metadata(path: Path) -> dataset_pb2.Dataset:
    """Load a Dataset just for its scalar metadata (name/description/id).

    Reactions are still parsed (the proto is monolithic), but the caller
    ignores them. Cheap enough at our scale (~50 ms each).
    """
    return message_helpers.load_message(str(path), dataset_pb2.Dataset)


def _classify(inputs: list[Path]) -> tuple[dict[str, list[Path]], list[Path]]:
    """Split inputs into (merge groups, singletons) by name."""
    groups: dict[str, list[Path]] = {spec.label: [] for spec in MERGE_SPECS}
    singletons: list[Path] = []
    for path in inputs:
        meta = _load_metadata(path)
        for spec in MERGE_SPECS:
            if spec.matches(meta):
                groups[spec.label].append(path)
                break
        else:
            singletons.append(path)
    return groups, singletons


def _convert_singleton(src: Path, repo_root: Path, dry_run: bool) -> str:
    dataset = message_helpers.load_message(str(src), dataset_pb2.Dataset)
    if not dataset.dataset_id:
        raise ValueError(f"{src}: missing dataset_id")
    out = _output_path(repo_root, dataset.dataset_id)
    if out.exists():
        return f"skip (exists)  {out.relative_to(repo_root)}"
    if dry_run:
        return f"would write    {out.relative_to(repo_root)}  ({len(dataset.reactions)} rxns)"
    out.parent.mkdir(parents=True, exist_ok=True)
    parquet_dataset.write_dataset(dataset, str(out))
    return f"wrote          {out.relative_to(repo_root)}  ({len(dataset.reactions)} rxns)"


def _convert_group(spec: MergeSpec, sources: list[Path], repo_root: Path, dry_run: bool) -> str:
    source_ids = []
    for src in sources:
        meta = _load_metadata(src)
        if not meta.dataset_id:
            raise ValueError(f"{src}: missing dataset_id")
        source_ids.append(meta.dataset_id)
    new_id = _derive_id(source_ids)
    out = _output_path(repo_root, new_id)
    if out.exists():
        return f"skip (exists)  {out.relative_to(repo_root)}  [{spec.label}, {len(sources)} sources]"
    if dry_run:
        return f"would merge    {out.relative_to(repo_root)}  [{spec.label}, {len(sources)} sources]"
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with parquet_dataset.DatasetWriter(
        str(out),
        name=spec.name,
        description=spec.description,
        dataset_id=new_id,
    ) as writer:
        for src in sorted(sources):  # deterministic write order
            ds = message_helpers.load_message(str(src), dataset_pb2.Dataset)
            writer.write_all(ds.reactions)
            total += len(ds.reactions)
            del ds  # release memory before loading the next input
    return f"wrote          {out.relative_to(repo_root)}  [{spec.label}, {len(sources)} sources, {total} rxns]"


def main() -> None:
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

    groups, singletons = _classify(inputs)
    logger.info(
        "Classified: %d singletons, %s",
        len(singletons),
        ", ".join(f"{len(v)}x {k}" for k, v in groups.items()),
    )

    for spec in MERGE_SPECS:
        sources = groups[spec.label]
        if not sources:
            logger.warning("No inputs matched merge spec %r", spec.label)
            continue
        logger.info(_convert_group(spec, sources, args.repo_root, args.dry_run))

    for src in singletons:
        logger.info(_convert_singleton(src, args.repo_root, args.dry_run))


if __name__ == "__main__":
    main()
