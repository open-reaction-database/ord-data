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

"""Tests for rewriting DateTime values as a date or a time with its offset."""

import datetime
from pathlib import Path

import normalize_dates
import pytest
from normalize_dates import (
    DETAILS,
    PIPELINE_DETAILS,
    PIPELINE_EMAIL,
    date_of,
    is_valid,
    normalize_dataset,
    normalize_reaction,
    pipeline_time,
)
from ord_schema import parquet
from ord_schema.proto import reaction_pb2
from rewrite_ambiguous_dates import make_event

DATASET_ID = "ord_dataset-00000000000000000000000000000000"
EVENT = make_event(
    reaction_pb2.Person(username="test", email="test@example.com"),
    datetime.datetime(2026, 10, 17, 17, 30, tzinfo=datetime.UTC),
)


def _reaction(
    reaction_id: str,
    created: str,
    *modified: tuple[str, bool],
    experiment_start: str = "",
) -> reaction_pb2.Reaction:
    """Builds a reaction from ``(time, set by the pipeline)`` record_modified pairs."""
    reaction = reaction_pb2.Reaction(reaction_id=reaction_id)
    reaction.provenance.record_created.time.value = created
    if experiment_start:
        reaction.provenance.experiment_start.value = experiment_start
    for value, pipeline in modified:
        event = reaction.provenance.record_modified.add()
        event.time.value = value
        if pipeline:
            event.person.email = PIPELINE_EMAIL
            event.details = PIPELINE_DETAILS
        else:
            event.person.email = "someone@example.com"
            event.details = "Clean up DOIs."
    return reaction


def _write(path: Path, *reactions: reaction_pb2.Reaction) -> Path:
    with parquet.DatasetWriter(
        path, name="test", description="test dataset", dataset_id=DATASET_ID
    ) as writer:
        writer.write_all(reactions)
    return path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-07-06", True),
        ("2024-07-06T23:25:41Z", True),
        ("2024-07-06T23:25:41+00:00", True),
        ("2024-07-06T23:25:41.780701-05:00", True),
        ("2024-07-06T23:25:41", False),
        ("2022-12-02 17:42:01.780701", False),
        ("Fri Oct 22 22:19:55 2021", False),
        ("10/19/2021, 12:16:25 PM", False),
        ("2024-13-01", False),
        ("2024-07-06T23:25:41+25:00", False),
        ("2024-07-06T23:25:41+00:60", False),
        ("", False),
    ],
)
def test_is_valid(value: str, expected: bool) -> None:
    assert is_valid(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Fri Oct 22 22:19:55 2021", "2021-10-22T22:19:55+00:00"),
        ("Thu Mar  4 01:02:03 2021", "2021-03-04T01:02:03+00:00"),
    ],
)
def test_pipeline_time(value: str, expected: str) -> None:
    assert pipeline_time(value) == expected


def test_pipeline_time_rejects_another_shape() -> None:
    with pytest.raises(ValueError, match=r"does not match format"):
        pipeline_time("2021-10-22 22:19:55")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("18/09/2024, 16:54:56", "2024-09-18"),
        ("18/08/2024 02:10:09", "2024-08-18"),
        ("10/19/2021, 12:16:25 PM", "2021-10-19"),
        ("10/19/2021", "2021-10-19"),
        ("Fri Oct 22 22:19:55 2021", "2021-10-22"),
        ("Thu Mar  4 23:59:59 2021", "2021-03-04"),
        ("2022-12-02 17:42:01.780701", "2022-12-02"),
        ("2022-12-02 19:09:01", "2022-12-02"),
        ("2025-12-19T10:27:58", "2025-12-19"),
        ("2025-01-01T00:00:00.5", "2025-01-01"),
    ],
)
def test_date_of(value: str, expected: str) -> None:
    assert date_of(value) == expected


def test_date_of_refuses_an_ambiguous_slash_date() -> None:
    with pytest.raises(ValueError, match=r"'07/01/2008' does not prove its day"):
        date_of("07/01/2008")


def test_date_of_refuses_an_unknown_shape() -> None:
    with pytest.raises(ValueError, match=r"no known shape matches '22 Oct 2021'"):
        date_of("22 Oct 2021")


def test_normalize_reaction() -> None:
    reaction = _reaction(
        "a",
        "2022-12-02 17:42:01.780701",
        ("Fri Oct 22 22:19:55 2021", True),
        ("Fri Oct 22 22:19:55 2021", False),
        ("2026-10-17T19:17:15+00:00", False),
        experiment_start="2021-01-02",
    )
    assert normalize_reaction(reaction) == [
        (
            "provenance.record_created.time",
            "2022-12-02 17:42:01.780701",
            "2022-12-02",
        ),
        (
            "provenance.record_modified[0].time",
            "Fri Oct 22 22:19:55 2021",
            "2021-10-22T22:19:55+00:00",
        ),
        (
            "provenance.record_modified[1].time",
            "Fri Oct 22 22:19:55 2021",
            "2021-10-22",
        ),
    ]
    assert reaction.provenance.experiment_start.value == "2021-01-02"
    assert reaction.provenance.record_modified[2].time.value == (
        "2026-10-17T19:17:15+00:00"
    )


def test_normalize_reaction_needs_both_marks_of_the_pipeline() -> None:
    reaction = _reaction("a", "2021-01-01", ("Fri Oct 22 22:19:55 2021", True))
    reaction.provenance.record_modified[0].details = "Something else."
    assert normalize_reaction(reaction)[0][2] == "2021-10-22"


def test_normalize_dataset(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "dataset.parquet",
        _reaction("a", "10/19/2021, 12:16:25 PM", ("Fri Oct 22 22:19:55 2021", True)),
        _reaction("b", "2021-10-19", ("2021-10-22T22:19:55+00:00", True)),
    )
    assert normalize_dataset(path, EVENT) == (1, 2)
    view = parquet.DatasetView(path)
    assert (view.name, view.description, view.dataset_id) == (
        "test",
        "test dataset",
        DATASET_ID,
    )
    assert list(view.iter_reaction_ids()) == ["a", "b"]
    first, second = view.reactions
    assert first.provenance.record_created.time.value == "2021-10-19"
    assert [event.time.value for event in first.provenance.record_modified] == [
        "2021-10-22T22:19:55+00:00",
        "2026-10-17T17:30:00+00:00",
    ]
    added = first.provenance.record_modified[-1]
    assert added.person == EVENT.person
    assert added.details == "\n".join(
        [
            DETAILS,
            "provenance.record_created.time: 10/19/2021, 12:16:25 PM -> 2021-10-19",
            (
                "provenance.record_modified[0].time: "
                "Fri Oct 22 22:19:55 2021 -> 2021-10-22T22:19:55+00:00"
            ),
        ]
    )
    # Nothing in the second needed rewriting, so it gains no event.
    assert len(second.provenance.record_modified) == 1


def test_normalize_dataset_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path / "dataset.parquet", _reaction("a", "2021-10-19 01:02:03"))
    before = path.read_bytes()
    assert normalize_dataset(path, EVENT, dry_run=True) == (1, 1)
    assert path.read_bytes() == before


def test_normalize_dataset_leaves_a_valid_dataset_alone(tmp_path: Path) -> None:
    path = _write(tmp_path / "dataset.parquet", _reaction("a", "2021-10-19"))
    # Rewriting the same content gives the same bytes, so only the file itself shows
    # whether it was replaced.
    before = path.stat().st_ino
    assert normalize_dataset(path, EVENT) == (0, 0)
    assert path.stat().st_ino == before


def test_normalize_dataset_refuses_an_unreadable_value(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "dataset.parquet",
        _reaction("a", "2021-10-19 01:02:03"),
        _reaction("b", "22 Oct 2021"),
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match=r"no known shape matches '22 Oct 2021'"):
        normalize_dataset(path, EVENT)
    assert path.read_bytes() == before


def test_normalize_dataset_checks_what_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        tmp_path / "dataset.parquet",
        _reaction("a", "2021-10-19 01:02:03"),
        _reaction("b", "2021-10-20 01:02:03"),
    )
    write = parquet.DatasetWriter.write

    def drop_b(self: parquet.DatasetWriter, reaction: reaction_pb2.Reaction) -> None:
        if reaction.reaction_id != "b":
            write(self, reaction)

    monkeypatch.setattr(parquet.DatasetWriter, "write", drop_b)
    with pytest.raises(RuntimeError, match=r"does not read back as planned"):
        normalize_dataset(path, EVENT)


def test_normalize_dataset_checks_every_value_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "dataset.parquet", _reaction("a", "2021-10-19 01:02:03"))
    monkeypatch.setattr(normalize_dates, "date_of", lambda value: "not a date")
    with pytest.raises(
        RuntimeError, match=r"still holds a DateTime ord-schema rejects"
    ):
        normalize_dataset(path, EVENT)
