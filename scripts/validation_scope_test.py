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

"""Tests for the Validation workflow's scope and leg decisions."""

import json
import re
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from validation_scope import (
    INTERPRETER,
    LOCK,
    WORKFLOW,
    Mode,
    Store,
    decide,
    environment,
    leg,
    main,
)

REPOSITORY = Path(__file__).resolve().parent.parent
PYPI = '{ registry = "https://pypi.org/simple" }'
DATASET = "data/00/ord_dataset-0.parquet"


def _lock(
    *,
    ord_schema: str = "0.9.6",
    ord_schema_source: str = PYPI,
    numpy: str = "2.4.6",
    pygithub: str = "2.5.0",
) -> str:
    """A uv.lock where ord-schema reaches numpy through rdkit, but not pygithub."""
    return textwrap.dedent(f"""\
        version = 1

        [[package]]
        name = "numpy"
        version = "{numpy}"
        source = {PYPI}

        [[package]]
        name = "ord-schema"
        version = "{ord_schema}"
        source = {ord_schema_source}
        dependencies = [
            {{ name = "rdkit" }},
        ]

        [[package]]
        name = "pygithub"
        version = "{pygithub}"
        source = {PYPI}

        [[package]]
        name = "rdkit"
        version = "2026.3.4"
        source = {PYPI}
        dependencies = [
            {{ name = "numpy" }},
        ]
        """)


def _git(*args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(files: Mapping[str, str | None]) -> str:
    """Commit ``files`` (None deletes one) and return the new commit."""
    for name, content in files.items():
        if content is None:
            Path(name).unlink()
            continue
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        Path(name).write_text(content)
    _git("add", "--all")
    _git("commit", "--quiet", "--allow-empty", "--message", "commit")
    return _git("rev-parse", "HEAD")


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A repository whose HEAD commit holds a lock, a workflow, and one dataset."""
    monkeypatch.chdir(tmp_path)
    _git("init", "--quiet")
    return _commit(
        {
            LOCK: _lock(),
            WORKFLOW: "name: Validation\n",
            INTERPRETER: "3.11\n",
            DATASET: "pointer\n",
        }
    )


@pytest.fixture
def no_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run outside any repository, so a decision that consults git fails."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


def _workflow() -> dict:
    # YAML 1.1 reads the bare key `on` as True.
    return yaml.safe_load((REPOSITORY / WORKFLOW).read_text())[True]


def test_environment_follows_dependencies_transitively() -> None:
    names = {name for name, _, _ in environment(_lock())}
    assert names == {"ord-schema", "rdkit", "numpy"}


def test_environment_of_this_repository() -> None:
    names = {name for name, _, _ in environment((REPOSITORY / LOCK).read_text())}
    assert {"ord-schema", "rdkit", "protobuf", "pyarrow"} <= names
    assert "pygithub" not in names


@pytest.mark.usefixtures("no_repository")
def test_workflow_dispatch_validates_the_corpus() -> None:
    assert decide("workflow_dispatch").mode == Mode.ALL


@pytest.mark.usefixtures("no_repository")
def test_pull_request_consults_git() -> None:
    with pytest.raises(subprocess.CalledProcessError, match=r"non-zero exit status"):
        decide("pull_request")


def test_unknown_event_raises() -> None:
    with pytest.raises(ValueError, match=r"no rule for schedule events"):
        decide("schedule")


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({LOCK: _lock(ord_schema="0.9.7")}, "ord-schema"),
        ({LOCK: _lock(numpy="2.5.1")}, "numpy"),
        (
            {LOCK: _lock(ord_schema_source='{ git = "https://example.com/o.git" }')},
            "ord-schema",
        ),
        ({INTERPRETER: "3.12\n"}, INTERPRETER),
        ({WORKFLOW: "name: Validation\non: push\n"}, WORKFLOW),
        # Changing the validator outranks changing data.
        ({LOCK: _lock(ord_schema="0.9.7"), DATASET: "edited\n"}, "ord-schema"),
    ],
)
@pytest.mark.usefixtures("base")
def test_pull_request_changing_the_validator_validates_the_corpus(
    files: dict[str, str], expected: str
) -> None:
    _commit(files)
    scope = decide("pull_request")
    assert scope.mode == Mode.ALL
    assert expected in scope.reason


@pytest.mark.usefixtures("base")
def test_pull_request_changing_data_validates_what_it_adds_or_modifies() -> None:
    _commit(
        {"data/11/ord_dataset-1.parquet": "a\n", "data/22/ord_dataset-2.parquet": "b\n"}
    )
    _commit(
        {
            DATASET: "edited\n",
            "data/11/ord_dataset-1.parquet": None,
            "data/22/ord_dataset-2.parquet": None,
            "data/33/ord_dataset-2.parquet": "b\n",  # renamed from data/22/
            "data/44/ord_dataset-4.parquet": "new\n",
        }
    )
    scope = decide("pull_request")
    assert scope.mode == Mode.CHANGED
    assert scope.datasets == (
        DATASET,
        "data/33/ord_dataset-2.parquet",
        "data/44/ord_dataset-4.parquet",
    )


@pytest.mark.parametrize(
    "files",
    [
        {LOCK: _lock(pygithub="2.6.0")},
        {"README.md": "docs\n"},
        {DATASET: None},
    ],
)
@pytest.mark.usefixtures("base")
def test_pull_request_changing_nothing_a_verdict_reads_validates_nothing(
    files: dict[str, str | None],
) -> None:
    _commit(files)
    assert decide("pull_request").mode == Mode.NONE


@pytest.mark.usefixtures("base")
def test_fork_pull_request_ignores_its_own_environment() -> None:
    _commit({LOCK: _lock(ord_schema="0.9.7"), INTERPRETER: "3.12\n"})
    assert decide("pull_request", fork=True).mode == Mode.NONE
    _commit({DATASET: "edited\n"})
    assert decide("pull_request", fork=True).datasets == (DATASET,)
    _commit({WORKFLOW: "name: Validation\non: push\n"})
    assert decide("pull_request", fork=True).mode == Mode.ALL


@pytest.mark.parametrize(
    ("files", "mode"),
    [
        ({DATASET: "edited\n"}, Mode.CHANGED),
        ({LOCK: _lock(ord_schema="0.9.7")}, Mode.ALL),
        ({WORKFLOW: "name: Validation\non: push\n"}, Mode.ALL),
        ({LOCK: _lock(pygithub="2.6.0")}, Mode.NONE),
    ],
)
def test_push(base: str, files: dict[str, str], mode: Mode) -> None:
    _commit(files)
    assert decide("push", before=base).mode == mode


def test_push_compares_against_before_not_the_parent(base: str) -> None:
    # A push of several commits: the data change is not in the last one.
    _commit({DATASET: "edited\n"})
    _commit({"README.md": "docs\n"})
    assert decide("push", before=base).datasets == (DATASET,)


def test_push_fetches_before_missing_from_a_shallow_checkout(
    base: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _commit({DATASET: "edited\n"})
    _commit({"README.md": "docs\n"})
    clone = tmp_path_factory.mktemp("clone")
    _git("clone", "--quiet", "--depth=2", f"file://{tmp_path}", str(clone))
    monkeypatch.chdir(clone)
    with pytest.raises(subprocess.CalledProcessError, match=r"non-zero exit status"):
        _git("cat-file", "-e", f"{base}^{{commit}}")
    assert decide("push", before=base).datasets == (DATASET,)


CORPUS = ["data/0a/ord_dataset-a.parquet", "data/5b/ord_dataset-b.parquet"]
LEG_FILTER = r"^data/[0-3][0-9a-f]/[^/]+\.parquet$"
LEG_GLOB = "data/[0-3]*/*.parquet"


def test_leg_of_the_corpus_reads_its_matrix_entry_from_the_mirror() -> None:
    share = leg(Mode.ALL, [], LEG_FILTER, LEG_GLOB, CORPUS)
    assert share.lfs_include == LEG_GLOB
    assert share.validator_filter == LEG_FILTER
    assert share.store == Store.HUGGINGFACE
    assert share.datasets == 1


def test_leg_of_the_corpus_refuses_a_filter_that_matches_nothing() -> None:
    with pytest.raises(ValueError, match=r"matches no dataset in the corpus"):
        leg(Mode.ALL, [], LEG_FILTER, LEG_GLOB, CORPUS[1:])


def test_leg_of_changed_datasets_reads_exactly_its_share_from_github() -> None:
    changed = ["data/0a/ord_dataset-a.parquet", "data/0c/x.y.parquet", CORPUS[1]]
    share = leg(Mode.CHANGED, changed, LEG_FILTER, LEG_GLOB, CORPUS)
    assert share.lfs_include == "data/0a/ord_dataset-a.parquet,data/0c/x.y.parquet"
    assert share.store == Store.GITHUB
    assert share.datasets == 2
    # validate_dataset.py applies the filter with re.search.
    selected = [
        "data/0a/ord_dataset-a.parquet",
        "data/0c/x.y.parquet",
    ]
    neighbors = [
        "data/0a/ord_dataset-a.parquet.bak",
        "x/data/0a/ord_dataset-a.parquet",
        "data/0c/xzy.parquet",
        CORPUS[1],
    ]
    pattern = re.compile(share.validator_filter)
    assert all(pattern.search(path) for path in selected)
    assert not any(pattern.search(path) for path in neighbors)


def test_leg_of_changed_datasets_may_be_empty() -> None:
    share = leg(Mode.CHANGED, [CORPUS[1]], LEG_FILTER, LEG_GLOB, CORPUS)
    assert share.datasets == 0
    assert share.lfs_include == ""


def test_leg_of_nothing_raises() -> None:
    with pytest.raises(ValueError, match=r"a none run has no legs"):
        leg(Mode.NONE, [], LEG_FILTER, LEG_GLOB, CORPUS)


def _run_main(monkeypatch: pytest.MonkeyPatch, output: Path, *argv: str) -> dict:
    monkeypatch.setattr(
        sys,
        "argv",
        ["validation_scope.py", *argv, f"--github_output={output}"],
    )
    output.unlink(missing_ok=True)
    main()
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.usefixtures("base")
def test_main_scope_writes_mode_and_datasets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit({LOCK: _lock(ord_schema="0.9.7"), DATASET: "edited\n"})
    output = tmp_path.parent / f"{tmp_path.name}-output"
    for fork, mode, datasets in [
        ("true", "changed", [DATASET]),
        ("false", "all", []),
        ("", "all", []),
    ]:
        outputs = _run_main(
            monkeypatch, output, "scope", "--event=pull_request", f"--fork={fork}"
        )
        assert outputs == {"mode": mode, "datasets": json.dumps(datasets)}


@pytest.mark.usefixtures("base")
def test_main_leg_writes_its_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path.parent / f"{tmp_path.name}-output"
    outputs = _run_main(
        monkeypatch,
        output,
        "leg",
        "--mode=changed",
        f"--datasets={json.dumps([DATASET])}",
        f"--filter={LEG_FILTER}",
        f"--lfs_include={LEG_GLOB}",
    )
    assert outputs == {
        "lfs_include": DATASET,
        "filter": f"^(?:{re.escape(DATASET)})$",
        "store": "github",
        "datasets": "1",
    }
    outputs = _run_main(
        monkeypatch,
        output,
        "leg",
        "--mode=all",
        f"--filter={LEG_FILTER}",
        f"--lfs_include={LEG_GLOB}",
    )
    assert outputs["store"] == "huggingface"
    assert outputs["datasets"] == "1"


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_trigger_paths_cover_what_scope_reads(event: str) -> None:
    paths = set(_workflow()[event]["paths"])
    assert {"data/**", WORKFLOW, INTERPRETER, LOCK} <= paths
