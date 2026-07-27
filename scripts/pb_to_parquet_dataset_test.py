# Copyright 2026 Open Reaction Database Project Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for the pb-to-Parquet converter."""

import logging

import pb_to_parquet_dataset as pb_to_parquet
import pytest
from ord_schema import message_helpers
from ord_schema import parquet as dataset_module
from ord_schema.proto import dataset_pb2, reaction_pb2


def _reaction(reaction_id: str, conversion: float = 50.0) -> reaction_pb2.Reaction:
    reaction = reaction_pb2.Reaction(reaction_id=reaction_id)
    reaction.outcomes.add().conversion.value = conversion
    return reaction


def _write_pb_gz(tmp_path, basename: str, ds: dataset_pb2.Dataset) -> str:
    path = str(tmp_path / basename)
    message_helpers.save_message(ds, path)
    return path


def test_single_input_passes_metadata_through(tmp_path):
    ds = dataset_pb2.Dataset(
        dataset_id="ord_dataset-abc",
        name="solo",
        description="single input",
        reactions=[_reaction("ord-0001"), _reaction("ord-0002")],
    )
    input_path = _write_pb_gz(tmp_path, "in.pb.gz", ds)
    output_path = str(tmp_path / "out.parquet")
    pb_to_parquet.main(pb_to_parquet.parse_args([input_path, "--output", output_path]))
    loaded = dataset_module.load_dataset(output_path)
    assert loaded.dataset_id == "ord_dataset-abc"
    assert loaded.name == "solo"
    assert loaded.description == "single input"
    assert [r.reaction_id for r in loaded.reactions] == ["ord-0001", "ord-0002"]


def test_multi_input_concatenates_and_uses_first_metadata(tmp_path):
    ds_a = dataset_pb2.Dataset(
        dataset_id="ord_dataset-first",
        name="first-name",
        description="first-desc",
        reactions=[_reaction("ord-a1")],
    )
    ds_b = dataset_pb2.Dataset(
        dataset_id="ord_dataset-second",
        name="second-name",
        description="second-desc",
        reactions=[_reaction("ord-b1"), _reaction("ord-b2")],
    )
    a_path = _write_pb_gz(tmp_path, "a.pb.gz", ds_a)
    b_path = _write_pb_gz(tmp_path, "b.pb.gz", ds_b)
    output_path = str(tmp_path / "out.parquet")
    pb_to_parquet.main(
        pb_to_parquet.parse_args([a_path, b_path, "--output", output_path])
    )
    loaded = dataset_module.load_dataset(output_path)
    assert loaded.dataset_id == "ord_dataset-first"
    assert loaded.name == "first-name"
    assert loaded.description == "first-desc"
    assert [r.reaction_id for r in loaded.reactions] == ["ord-a1", "ord-b1", "ord-b2"]


def test_overrides_take_precedence(tmp_path):
    ds = dataset_pb2.Dataset(name="input-name", reactions=[_reaction("ord-0001")])
    input_path = _write_pb_gz(tmp_path, "in.pb.gz", ds)
    output_path = str(tmp_path / "out.parquet")
    pb_to_parquet.main(
        pb_to_parquet.parse_args(
            [
                input_path,
                "--output",
                output_path,
                "--name",
                "override-name",
                "--description",
                "override-desc",
                "--dataset-id",
                "ord_dataset-new",
            ]
        )
    )
    loaded = dataset_module.load_metadata(output_path)
    assert loaded.dataset_id == "ord_dataset-new"
    assert loaded.name == "override-name"
    assert loaded.description == "override-desc"


def test_mismatched_metadata_warns(tmp_path, caplog):
    ds_a = dataset_pb2.Dataset(
        dataset_id="ord_dataset-foo",
        name="foo",
        description="d",
        reactions=[_reaction("ord-a1")],
    )
    ds_b = dataset_pb2.Dataset(
        dataset_id="ord_dataset-bar",
        name="bar",
        description="other",
        reactions=[_reaction("ord-b1")],
    )
    a_path = _write_pb_gz(tmp_path, "a.pb.gz", ds_a)
    b_path = _write_pb_gz(tmp_path, "b.pb.gz", ds_b)
    output_path = str(tmp_path / "out.parquet")
    with caplog.at_level(logging.WARNING, logger="pb_to_parquet_dataset"):
        pb_to_parquet.main(
            pb_to_parquet.parse_args([a_path, b_path, "--output", output_path])
        )
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "dataset_id" in messages
    assert "name" in messages
    assert "description" in messages


def test_empty_inputs_raise(tmp_path):
    ds = dataset_pb2.Dataset(name="empty", description="d")  # No reactions.
    input_path = _write_pb_gz(tmp_path, "in.pb.gz", ds)
    output_path = str(tmp_path / "out.parquet")
    with pytest.raises(ValueError, match="no reactions"):
        pb_to_parquet.main(
            pb_to_parquet.parse_args([input_path, "--output", output_path])
        )
