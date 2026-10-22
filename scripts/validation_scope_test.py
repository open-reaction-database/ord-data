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

"""Tests for the Validation workflow's scope decision."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from validation_scope import LABEL, LOCK, WORKFLOW, decide, environment, main

REPOSITORY = Path(__file__).resolve().parent.parent
PYPI = '{ registry = "https://pypi.org/simple" }'


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


def _commit(files: dict[str, str]) -> str:
    for name, content in files.items():
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
            ".python-version": "3.11\n",
            "data/00/ord_dataset-0.parquet": "pointer\n",
        }
    )


def test_environment_follows_dependencies_transitively() -> None:
    names = {name for name, _, _ in environment(_lock())}
    assert names == {"ord-schema", "rdkit", "numpy"}


def test_environment_of_this_repository() -> None:
    names = {name for name, _, _ in environment((REPOSITORY / LOCK).read_text())}
    assert {"ord-schema", "rdkit", "protobuf", "pyarrow"} <= names
    assert "pygithub" not in names


@pytest.fixture
def no_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run outside any repository, so a decision that consults git fails."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


@pytest.mark.usefixtures("no_repository")
def test_workflow_dispatch_always_validates() -> None:
    assert decide("workflow_dispatch") == "manual dispatch"


@pytest.mark.usefixtures("no_repository")
def test_labeled_validates_only_for_the_label() -> None:
    assert decide("pull_request", action="labeled", added_label=LABEL)
    assert (
        decide(
            "pull_request",
            action="labeled",
            added_label="documentation",
            labels=[LABEL, "documentation"],
        )
        is None
    )


@pytest.mark.usefixtures("no_repository")
def test_pull_request_carrying_the_label_validates() -> None:
    assert decide("pull_request", action="synchronize", labels=[LABEL])


@pytest.mark.usefixtures("no_repository")
def test_pull_request_without_the_label_consults_git() -> None:
    with pytest.raises(subprocess.CalledProcessError, match=r"non-zero exit status"):
        decide("pull_request", action="opened")


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({LOCK: _lock(ord_schema="0.9.7")}, "ord-schema"),
        ({LOCK: _lock(numpy="2.5.1")}, "numpy"),
        (
            {LOCK: _lock(ord_schema_source='{ git = "https://example.com/o.git" }')},
            "ord-schema",
        ),
        ({".python-version": "3.12\n"}, ".python-version"),
        ({WORKFLOW: "name: Validation\non: push\n"}, WORKFLOW),
    ],
)
@pytest.mark.usefixtures("base")
def test_pull_request_changing_the_validator_validates(
    files: dict[str, str], expected: str
) -> None:
    _commit(files)
    reason = decide("pull_request", action="synchronize")
    assert reason
    assert expected in reason


@pytest.mark.parametrize(
    "files",
    [
        {LOCK: _lock(pygithub="2.6.0")},
        {"data/00/ord_dataset-0.parquet": "edited\n"},
        {"README.md": "docs\n"},
    ],
)
@pytest.mark.usefixtures("base")
def test_pull_request_leaving_the_validator_alone_does_not_validate(
    files: dict[str, str],
) -> None:
    _commit(files)
    assert decide("pull_request", action="opened") is None


@pytest.mark.usefixtures("base")
def test_fork_pull_request_ignores_its_own_environment() -> None:
    _commit({LOCK: _lock(ord_schema="0.9.7"), ".python-version": "3.12\n"})
    assert decide("pull_request", action="synchronize", fork=True) is None
    _commit({WORKFLOW: "name: Validation\non: push\n"})
    assert decide("pull_request", action="synchronize", fork=True)


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"data/00/ord_dataset-0.parquet": "edited\n"}, "data/00"),
        ({LOCK: _lock(ord_schema="0.9.7")}, "ord-schema"),
        ({WORKFLOW: "name: Validation\non: push\n"}, WORKFLOW),
    ],
)
def test_push_changing_data_or_validator_validates(
    base: str, files: dict[str, str], expected: str
) -> None:
    _commit(files)
    reason = decide("push", before=base)
    assert reason
    assert expected in reason


def test_push_compares_against_before_not_the_parent(base: str) -> None:
    # A push of several commits: the data change is not in the last one.
    _commit({"data/00/ord_dataset-0.parquet": "edited\n"})
    _commit({"README.md": "docs\n"})
    assert decide("push", before=base)


def test_push_leaving_the_validator_alone_does_not_validate(base: str) -> None:
    _commit({LOCK: _lock(pygithub="2.6.0")})
    assert decide("push", before=base) is None


def test_main_writes_the_step_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    labels = f'[\n  "{LABEL}"\n]'  # toJSON pretty-prints
    for argv, expected in [
        (["--event=pull_request", "--action=opened", f"--labels={labels}"], "true"),
        (["--event=pull_request", "--action=labeled", "--added_label=x"], "false"),
    ]:
        monkeypatch.setattr(
            sys, "argv", ["validation_scope.py", *argv, f"--github_output={output}"]
        )
        main()
        assert output.read_text().splitlines()[-1] == f"validate_corpus={expected}"


def test_unknown_event_raises() -> None:
    with pytest.raises(ValueError, match=r"no rule for schedule events"):
        decide("schedule")


def test_concurrency_group_names_the_label() -> None:
    workflow = (REPOSITORY / WORKFLOW).read_text()
    assert f"github.event.label.name != '{LABEL}'" in workflow
