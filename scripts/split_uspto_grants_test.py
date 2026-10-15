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

"""Tests for the USPTO grants year split."""

import re
from pathlib import Path

import pytest
from ord_schema import parquet
from ord_schema.proto import reaction_pb2
from split_uspto_grants import (
    MonthlySource,
    derive_dataset_id,
    describe_year,
    output_path,
    parse_month,
    plan_years,
    write_years,
)


def _source(year: int, month: int, *reaction_ids: str) -> MonthlySource:
    return MonthlySource(
        dataset_id=f"ord_dataset-{year:04d}{month:02d}{'0' * 26}",
        year=year,
        month=month,
        reaction_ids=reaction_ids,
    )


def test_parse_month() -> None:
    assert parse_month("uspto-grants-1976_01") == (1976, 1)
    assert parse_month("uspto-grants-2016_09") == (2016, 9)


@pytest.mark.parametrize(
    "name", ["uspto-grants", "uspto-grants-1976", "uspto-grants-1976_13", "x"]
)
def test_parse_month_rejects_other_names(name: str) -> None:
    with pytest.raises(ValueError, match=r"not a monthly uspto-grants name"):
        parse_month(name)


def test_derive_dataset_id_is_well_formed_and_order_independent() -> None:
    dataset_id = derive_dataset_id(["ord_dataset-b", "ord_dataset-a"])
    assert re.fullmatch(r"ord_dataset-[0-9a-f]{32}", dataset_id)
    assert dataset_id == derive_dataset_id(["ord_dataset-a", "ord_dataset-b"])
    assert dataset_id != derive_dataset_id(["ord_dataset-a"])


def test_describe_year_full_year() -> None:
    description = describe_year(1976, list(range(1, 13)))
    assert "granted in 1976." in description


def test_describe_year_partial_year() -> None:
    description = describe_year(2016, list(range(1, 10)))
    assert "granted from January through September 2016." in description


def test_describe_year_rejects_gaps() -> None:
    with pytest.raises(ValueError, match=r"not a contiguous run"):
        describe_year(2016, [1, 2, 4])


def test_plan_years_orders_months_and_derives_ids() -> None:
    sources = [
        _source(1977, 1, "ord-c"),
        _source(1976, 2, "ord-b"),
        _source(1976, 1, "ord-a"),
    ]
    plans = plan_years(sources)
    assert [plan.year for plan in plans] == [1976, 1977]
    assert [month.month for month in plans[0].months] == [1, 2]
    assert plans[0].reaction_ids == ["ord-a", "ord-b"]
    assert plans[0].name == "uspto-grants-1976"
    assert plans[0].dataset_id == derive_dataset_id(
        [sources[1].dataset_id, sources[2].dataset_id]
    )


def test_plan_years_rejects_duplicate_month() -> None:
    with pytest.raises(ValueError, match=r"1976-01 appears in more than one"):
        plan_years([_source(1976, 1, "ord-a"), _source(1976, 1, "ord-b")])


def _write_source(path: Path, reaction_ids: list[str]) -> parquet.DatasetView:
    with parquet.DatasetWriter(
        path, name="uspto-grants", description="all of it", dataset_id=None
    ) as writer:
        for reaction_id in reaction_ids:
            reaction = reaction_pb2.Reaction(reaction_id=reaction_id)
            reaction.provenance.patent = f"US{reaction_id}"
            writer.write(reaction)
    return parquet.DatasetView(path)


def test_write_years_splits_in_calendar_order(tmp_path: Path) -> None:
    # Source order mirrors the consolidated file: months in an arbitrary order.
    view = _write_source(tmp_path / "source.parquet", ["d", "a", "b", "c"])
    plans = plan_years(
        [
            _source(1977, 1, "d"),
            _source(1976, 2, "c"),
            _source(1976, 1, "a", "b"),
        ]
    )
    paths = write_years(view, plans, tmp_path)
    assert paths == [output_path(tmp_path, plan.dataset_id) for plan in plans]
    first = parquet.DatasetView(paths[0])
    assert first.name == "uspto-grants-1976"
    assert first.dataset_id == plans[0].dataset_id
    assert list(first.iter_reaction_ids()) == ["a", "b", "c"]
    assert first.reactions[2].provenance.patent == "USc"
    assert list(parquet.DatasetView(paths[1]).iter_reaction_ids()) == ["d"]


def test_write_years_rejects_unassigned_reactions(tmp_path: Path) -> None:
    view = _write_source(tmp_path / "source.parquet", ["a", "b"])
    with pytest.raises(ValueError, match=r"1 source reactions have no month"):
        write_years(view, plan_years([_source(1976, 1, "a")]), tmp_path)
    assert not list(tmp_path.glob("data/*/*.parquet"))


def test_write_years_rejects_reactions_missing_from_source(tmp_path: Path) -> None:
    view = _write_source(tmp_path / "source.parquet", ["a"])
    with pytest.raises(ValueError, match=r"1 monthly reactions are not in the source"):
        write_years(view, plan_years([_source(1976, 1, "a", "b")]), tmp_path)


def test_write_years_rejects_reaction_in_two_months(tmp_path: Path) -> None:
    view = _write_source(tmp_path / "source.parquet", ["a"])
    sources = [_source(1976, 1, "a"), _source(1976, 2, "a")]
    with pytest.raises(ValueError, match=r"1 reactions appear in more than one month"):
        write_years(view, plan_years(sources), tmp_path)
