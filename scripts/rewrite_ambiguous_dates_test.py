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

"""Tests for the ambiguous slash date rewrite."""

import datetime
from pathlib import Path

import pytest
from ord_schema import parquet, validations
from ord_schema.proto import reaction_pb2
from rewrite_ambiguous_dates import (
    DETAILS,
    describe,
    is_ambiguous,
    iter_date_times,
    make_event,
    rewrite_dataset,
    to_iso,
    witnessed_day_first,
)

DATASET_ID = "ord_dataset-00000000000000000000000000000000"
EVENT = make_event(
    reaction_pb2.Person(username="test", email="test@example.com"),
    # 13:30 four hours behind UTC.
    datetime.datetime(
        2026, 10, 17, 13, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=-4))
    ),
)


def _reaction(
    reaction_id: str, created: str, *modified: str, calibrated: str = ""
) -> reaction_pb2.Reaction:
    reaction = reaction_pb2.Reaction(reaction_id=reaction_id)
    reaction.provenance.record_created.time.value = created
    for value in modified:
        reaction.provenance.record_modified.add().time.value = value
    if calibrated:
        outcome = reaction.outcomes.add()
        outcome.analyses["hplc"].instrument_last_calibrated.value = calibrated
    return reaction


def _write(path: Path, *reactions: reaction_pb2.Reaction) -> Path:
    with parquet.DatasetWriter(
        path, name="test", description="test dataset", dataset_id=DATASET_ID
    ) as writer:
        writer.write_all(reactions)
    return path


def _values(path: Path) -> list[list[str]]:
    return [
        [date_time.value for _, date_time in iter_date_times(reaction)]
        for reaction in parquet.DatasetView(path).reactions
    ]


def _event(*lines: str) -> reaction_pb2.RecordEvent:
    event = reaction_pb2.RecordEvent()
    event.CopyFrom(EVENT)
    event.details = "\n".join([DETAILS, *lines])
    return event


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("07/01/2008", True),
        ("2/4/2021, 11:24:34 AM", True),
        ("07/07/2024", True),
        ("18/09/2024, 16:54:56", False),
        ("10/19/2021, 12:16:25 PM", False),
        ("2024-07-01", False),
        ("Fri Oct 22 22:19:55 2021", False),
        ("2024/07/01", False),
    ],
)
def test_is_ambiguous(value: str, expected: bool) -> None:
    assert is_ambiguous(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("18/09/2024, 16:54:56", True),
        ("10/19/2021, 12:16:25 PM", False),
        ("07/01/2008", None),
        ("2024-07-01", None),
    ],
)
def test_witnessed_day_first(value: str, expected: bool | None) -> None:
    assert witnessed_day_first(value) is expected


@pytest.mark.parametrize(
    ("value", "day_first", "expected"),
    [
        ("2/4/2021, 11:24:34 AM", False, "2021-02-04T11:24:34"),
        ("8/9/2023, 10:44:35 PM", False, "2023-08-09T22:44:35"),
        ("6/2/2021, 8:10:58 AM", False, "2021-06-02T08:10:58"),
        ("07/06/2024, 23:25:41", False, "2024-07-06T23:25:41"),
        ("05/12/2024, 15:29:23", True, "2024-12-05T15:29:23"),
        ("05/08/2021 10:29:17", True, "2021-08-05T10:29:17"),
        ("07/01/2008", False, "2008-07-01"),
    ],
)
def test_to_iso(value: str, day_first: bool, expected: str) -> None:
    assert to_iso(value, day_first=day_first) == expected


def test_to_iso_rejects_an_unknown_shape() -> None:
    with pytest.raises(ValueError, match=r"no slash date shape matches '07/01/2008 "):
        to_iso("07/01/2008 at noon", day_first=False)


@pytest.mark.parametrize(
    "value", ["07/01/2008, 10:00:00 +02:00", "07/01/2008 10:00:00Z"]
)
def test_rewrite_dataset_refuses_rather_than_drop_an_offset(
    tmp_path: Path, value: str
) -> None:
    path = _write(tmp_path / "dataset.parquet", _reaction("a", value))
    before = path.read_bytes()
    with pytest.raises(ValueError, match=r"no slash date shape matches"):
        rewrite_dataset(path, EVENT, day_first={DATASET_ID: False})
    assert path.read_bytes() == before


def test_iter_date_times_names_each_path() -> None:
    reaction = _reaction("a", "1", "2", "3", calibrated="4")
    reaction.provenance.experiment_start.value = "0"
    # Protobuf iterates these four keys out of sorted order.
    for key in ("nmr", "lcms", "gc"):
        reaction.outcomes[0].analyses[key].instrument_last_calibrated.value = key
    assert [(path, dt.value) for path, dt in iter_date_times(reaction)] == [
        ('outcomes[0].analyses["gc"].instrument_last_calibrated', "gc"),
        ('outcomes[0].analyses["hplc"].instrument_last_calibrated', "4"),
        ('outcomes[0].analyses["lcms"].instrument_last_calibrated', "lcms"),
        ('outcomes[0].analyses["nmr"].instrument_last_calibrated', "nmr"),
        ("provenance.experiment_start", "0"),
        ("provenance.record_created.time", "1"),
        ("provenance.record_modified[0].time", "2"),
        ("provenance.record_modified[1].time", "3"),
    ]


def test_make_event() -> None:
    assert EVENT.time.value == "2026-10-17T17:30:00+00:00"
    assert (EVENT.person.username, EVENT.person.email) == ("test", "test@example.com")
    assert not EVENT.details


def test_make_event_refuses_a_naive_time() -> None:
    with pytest.raises(ValueError, match=r"2026-10-17 13:30:00 has no time zone"):
        make_event(EVENT.person, datetime.datetime(2026, 10, 17, 13, 30))  # noqa: DTZ001


def test_describe() -> None:
    changes = [("a.time", "07/01/2008", "2008-07-01"), ("b[0]", "1/2/3", "x")]
    assert describe(changes) == (
        f"{DETAILS}\na.time: 07/01/2008 -> 2008-07-01\nb[0]: 1/2/3 -> x"
    )


def test_rewrite_dataset(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "dataset.parquet",
        _reaction("a", "05/12/2024, 15:29:23", "Fri Dec  6 10:00:00 2024"),
        _reaction("b", "18/09/2024, 16:54:56", calibrated="01/02/2024"),
        _reaction("c", "2024-12-05T15:29:23"),
    )
    assert rewrite_dataset(path, EVENT, day_first={DATASET_ID: True}) == 2
    view = parquet.DatasetView(path)
    assert (view.name, view.description, view.dataset_id) == (
        "test",
        "test dataset",
        DATASET_ID,
    )
    assert list(view.iter_reaction_ids()) == ["a", "b", "c"]
    assert _values(path) == [
        [
            "2024-12-05T15:29:23",
            "Fri Dec  6 10:00:00 2024",
            "2026-10-17T17:30:00+00:00",
        ],
        # Fields in number order: outcomes come before provenance.
        ["2024-02-01", "18/09/2024, 16:54:56", "2026-10-17T17:30:00+00:00"],
        ["2024-12-05T15:29:23"],
    ]
    # One event each for the reactions it rewrote, after any they had.
    assert [list(r.provenance.record_modified) for r in view.reactions] == [
        [
            reaction_pb2.RecordEvent(time={"value": "Fri Dec  6 10:00:00 2024"}),
            _event(
                "provenance.record_created.time: "
                "05/12/2024, 15:29:23 -> 2024-12-05T15:29:23"
            ),
        ],
        [
            _event(
                'outcomes[0].analyses["hplc"].instrument_last_calibrated: '
                "01/02/2024 -> 2024-02-01"
            )
        ],
        [],
    ]


@pytest.mark.xfail(
    raises=TypeError,
    strict=True,
    reason="ord-schema 0.8.3 cannot order a naive timestamp against an aware one",
)
def test_a_rewritten_reaction_validates(tmp_path: Path) -> None:
    reaction = _reaction("ord-" + "0" * 32, "07/01/2008")
    reaction.provenance.record_created.person.username = "test"
    reaction.provenance.record_created.person.email = "test@example.com"
    path = _write(tmp_path / "dataset.parquet", reaction)
    rewrite_dataset(path, EVENT, day_first={DATASET_ID: False})
    provenance = parquet.DatasetView(path).reactions[0].provenance
    output = validations.validate_message(provenance, raise_on_error=False)
    assert output.errors == []


def test_rewrite_dataset_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path / "dataset.parquet", _reaction("a", "07/01/2008"))
    before = path.read_bytes()
    assert (
        rewrite_dataset(path, EVENT, day_first={DATASET_ID: False}, dry_run=True) == 1
    )
    assert path.read_bytes() == before


def test_rewrite_dataset_leaves_an_unambiguous_dataset_alone(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "dataset.parquet", _reaction("a", "10/19/2021, 12:16:25 PM")
    )
    before = path.read_bytes()
    assert rewrite_dataset(path, EVENT, day_first={}) == 0
    assert path.read_bytes() == before


def test_rewrite_dataset_refuses_an_unsettled_order(tmp_path: Path) -> None:
    path = _write(tmp_path / "dataset.parquet", _reaction("a", "07/01/2008"))
    with pytest.raises(ValueError, match=r"no order is settled for ord_dataset-0+$"):
        rewrite_dataset(path, EVENT, day_first={})


def test_rewrite_dataset_checks_what_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        tmp_path / "dataset.parquet",
        _reaction("a", "07/01/2008"),
        _reaction("b", "07/02/2008"),
    )
    write = parquet.DatasetWriter.write

    def drop_b(self: parquet.DatasetWriter, reaction: reaction_pb2.Reaction) -> None:
        if reaction.reaction_id != "b":
            write(self, reaction)

    monkeypatch.setattr(parquet.DatasetWriter, "write", drop_b)
    with pytest.raises(RuntimeError, match=r"does not read back as planned"):
        rewrite_dataset(path, EVENT, day_first={DATASET_ID: False})


def test_rewrite_dataset_refuses_an_order_its_values_contradict(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "dataset.parquet",
        _reaction("a", "07/01/2008"),
        _reaction("b", "07/13/2008"),
    )
    with pytest.raises(ValueError, match=r"day first, but 1 of its values prove"):
        rewrite_dataset(path, EVENT, day_first={DATASET_ID: True})
