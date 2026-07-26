"""Tests for the Hugging Face mirror script."""

from pathlib import Path

import pytest
from upload_to_huggingface import (
    USPTO_GRANTS_ID,
    USPTO_MIT_SPLITS,
    build_configs,
    parse_name_status,
)


def test_parse_name_status_added_and_modified_become_uploads() -> None:
    plan = parse_name_status("A\tdata/00/ord_dataset-a.parquet\nM\tREADME.md\n")
    assert plan.uploads == ["data/00/ord_dataset-a.parquet", "README.md"]
    assert plan.deletions == []


def test_parse_name_status_deletion_becomes_deletion() -> None:
    plan = parse_name_status("D\tdata/00/ord_dataset-a.parquet\n")
    assert plan.uploads == []
    assert plan.deletions == ["data/00/ord_dataset-a.parquet"]


def test_parse_name_status_rename_splits_into_delete_and_upload() -> None:
    """A rename must delete the old path, not just upload the new one.

    The mirror has no rename operation, so a rename that only uploaded the
    destination would leave the source orphaned on Hugging Face.
    """
    plan = parse_name_status("R100\tdata/00/old.parquet\tdata/11/new.parquet\n")
    assert plan.deletions == ["data/00/old.parquet"]
    assert plan.uploads == ["data/11/new.parquet"]


def test_parse_name_status_copy_uploads_destination_only() -> None:
    plan = parse_name_status("C75\tdata/00/src.parquet\tdata/11/dst.parquet\n")
    assert plan.uploads == ["data/11/dst.parquet"]
    assert plan.deletions == []


def test_parse_name_status_skips_blank_and_unrecognized_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = parse_name_status("\n  \nU\tdata/00/conflicted.parquet\n")
    assert plan.uploads == []
    assert plan.deletions == []
    assert "Skipping unrecognized status line" in capsys.readouterr().err


def _touch_dataset(repo_root: Path, dataset_id: str) -> str:
    """Creates an empty parquet file for ``dataset_id`` and returns its repo path."""
    shard = dataset_id.removeprefix("ord_dataset-")[:2]
    path = repo_root / "data" / shard / f"{dataset_id}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path.relative_to(repo_root).as_posix()


def test_build_configs_default_excludes_named_datasets(tmp_path: Path) -> None:
    plain = _touch_dataset(tmp_path, "ord_dataset-00005539a1e04c809a9a78647bea649c")
    _touch_dataset(tmp_path, USPTO_GRANTS_ID)
    for dataset_id in USPTO_MIT_SPLITS.values():
        _touch_dataset(tmp_path, dataset_id)

    configs = build_configs(tmp_path)

    default = configs[0]
    assert default["config_name"] == "default"
    assert default["default"] is True
    assert default["data_files"] == [plain]


def test_build_configs_emits_named_uspto_configs(tmp_path: Path) -> None:
    grants = _touch_dataset(tmp_path, USPTO_GRANTS_ID)
    mit = {
        split: _touch_dataset(tmp_path, dataset_id)
        for split, dataset_id in USPTO_MIT_SPLITS.items()
    }

    by_name = {config["config_name"]: config for config in build_configs(tmp_path)}

    assert by_name["uspto-grants"]["data_files"] == [grants]
    assert by_name["uspto-mit"]["data_files"] == [
        {"split": split, "path": mit[split]} for split in USPTO_MIT_SPLITS
    ]


def test_build_configs_omits_named_configs_when_datasets_absent(
    tmp_path: Path,
) -> None:
    """Partial coverage must not emit a config referencing a missing split."""
    _touch_dataset(tmp_path, "ord_dataset-00005539a1e04c809a9a78647bea649c")
    _touch_dataset(tmp_path, USPTO_MIT_SPLITS["train"])

    names = {config["config_name"] for config in build_configs(tmp_path)}

    assert "uspto-grants" not in names
    assert "uspto-mit" not in names


def test_build_configs_emits_one_config_per_dataset(tmp_path: Path) -> None:
    dataset_ids = [
        "ord_dataset-00005539a1e04c809a9a78647bea649c",
        "ord_dataset-11b3c3b41eda49e196ec983a65d3b2c0",
    ]
    paths = {
        dataset_id: _touch_dataset(tmp_path, dataset_id) for dataset_id in dataset_ids
    }

    by_name = {config["config_name"]: config for config in build_configs(tmp_path)}

    for dataset_id, path in paths.items():
        assert by_name[dataset_id]["data_files"] == [path]


def test_build_configs_on_empty_tree_yields_only_default(tmp_path: Path) -> None:
    configs = build_configs(tmp_path)
    assert configs == [{"config_name": "default", "default": True, "data_files": []}]
