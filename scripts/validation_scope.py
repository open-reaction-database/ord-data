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

"""Decide what a Validation workflow run validates, and which leg takes what.

``scope`` decides for the triggering event. The whole corpus is validated when
the verdict on unchanged data could change: on a manual dispatch, or when
validation.yml or the environment changes. Otherwise the run validates the
datasets it adds or modifies under data/, and nothing when there are none.
validate_dataset.py judges each dataset on its own, so the rest of the corpus
has no bearing on a changed dataset's verdict.

The environment is the interpreter named by .python-version plus ord-schema and
every package it depends on, as uv.lock resolves them. A lock change confined to
other packages leaves it alone. A fork's pull request is validated in its base
branch's environment, so the fork's own uv.lock and .python-version are not
under test.

``leg`` narrows one validate_parquet leg to its share of that decision: which
objects to pull, from which LFS store, and the filter to hand the validator.

Both write their results to the step output file. They run on the system
interpreter before anything is installed, so this uses the standard library
only.
"""

import argparse
import enum
import itertools
import json
import re
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DATA = "data"
WORKFLOW = ".github/workflows/validation.yml"
INTERPRETER = ".python-version"
LOCK = "uv.lock"
VALIDATOR = "ord-schema"
# Characters git lfs gives meaning in an --include list.
_LFS_PATTERN_CHARACTERS = re.compile(r"[*?\[\],\\]")


class Mode(enum.StrEnum):
    """How much of the corpus a run validates."""

    ALL = "all"
    CHANGED = "changed"
    NONE = "none"


class Store(enum.StrEnum):
    """The LFS store a leg reads from first."""

    # .lfsconfig points reads at the mirror. It syncs from GitHub after each
    # merge to main, so it lacks whatever a pull request or the latest merge
    # added.
    HUGGINGFACE = "huggingface"
    GITHUB = "github"


@dataclass(frozen=True)
class Scope:
    """What a run validates, and why."""

    mode: Mode
    reason: str
    datasets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Leg:
    """One validate_parquet leg's share of a run."""

    lfs_include: str
    validator_filter: str
    store: Store
    datasets: int


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def _ensure_commit(revision: str) -> None:
    """Fetch ``revision`` unless the shallow checkout already holds it."""
    present = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    if present.returncode:
        _git("fetch", "--no-tags", "--depth=1", "origin", revision)


def _changed(base: str, paths: Sequence[str], *options: str) -> list[str]:
    """Return the files under ``paths`` that differ between ``base`` and HEAD."""
    return _git(
        "diff", "--name-only", "--no-renames", *options, base, "HEAD", "--", *paths
    ).splitlines()


def environment(lock: str) -> frozenset[tuple[str, str, str]]:
    """Return ord-schema and every package it depends on, as a uv.lock resolves them.

    Args:
        lock: The text of a uv.lock file.

    Returns:
        One (name, version, source) entry per resolved package, with the source
        table as canonical JSON so that a switch from PyPI to a git revision
        counts even at an unchanged version.
    """
    packages: dict[str, list[dict]] = {}
    for package in tomllib.loads(lock).get("package", []):
        packages.setdefault(package["name"], []).append(package)
    resolved = set()
    pending, visited = [VALIDATOR], set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        for package in packages.get(name, []):
            source = json.dumps(package.get("source"), sort_keys=True)
            resolved.add((name, package.get("version", ""), source))
            dependencies = itertools.chain(
                package.get("dependencies", []),
                *package.get("optional-dependencies", {}).values(),
            )
            pending.extend(dependency["name"] for dependency in dependencies)
    return frozenset(resolved)


def decide(event: str, *, fork: bool = False, before: str = "") -> Scope:
    """Return what this run validates.

    Args:
        event: The triggering event: push, pull_request, or workflow_dispatch.
        fork: Whether the pull request comes from a fork.
        before: The commit a push moved main from.

    Returns:
        The whole corpus, the datasets the event added or modified, or nothing.

    Raises:
        ValueError: If ``event`` is not one the workflow triggers on.
    """
    if event == "workflow_dispatch":
        return Scope(Mode.ALL, "manual dispatch")
    if event == "pull_request":
        # HEAD is the merge commit, and its first parent the base it was made on.
        base, environment_under_test = "HEAD^1", not fork
    elif event == "push":
        _ensure_commit(before)
        base, environment_under_test = before, True
    else:
        raise ValueError(f"no rule for {event} events")
    watched = [WORKFLOW, INTERPRETER] if environment_under_test else [WORKFLOW]
    if changed := _changed(base, watched):
        return Scope(Mode.ALL, f"changes {', '.join(changed)}")
    if environment_under_test:
        difference = environment(_git("show", f"{base}:{LOCK}")) ^ environment(
            _git("show", f"HEAD:{LOCK}")
        )
        if difference:
            names = sorted({name for name, _, _ in difference})
            return Scope(Mode.ALL, f"changes {', '.join(names)} in {LOCK}")
    # A deleted dataset leaves nothing to validate.
    if datasets := tuple(_changed(base, [DATA], "--diff-filter=d")):
        return Scope(Mode.CHANGED, f"changes {len(datasets)} datasets", datasets)
    return Scope(Mode.NONE, "changes nothing a verdict depends on")


def leg(
    mode: Mode,
    datasets: Sequence[str],
    leg_filter: str,
    lfs_include: str,
    corpus: Sequence[str],
) -> Leg:
    """Return one leg's share of a run.

    Args:
        mode: What the run validates; never ``Mode.NONE``, which runs no legs.
        datasets: The changed datasets, when ``mode`` is ``Mode.CHANGED``.
        leg_filter: The leg's matrix filter, a regex over dataset paths.
        lfs_include: The leg's matrix LFS glob, equivalent to ``leg_filter``.
        corpus: Every path under data/.

    Returns:
        For the whole corpus, the leg's own glob and filter, read from the
        Hugging Face mirror. For changed datasets, the ones the leg's filter
        selects, by exact path, read from GitHub; possibly none.

    Raises:
        ValueError: If ``mode`` is ``Mode.NONE``, if the leg's filter selects
            nothing from the whole corpus, or if a changed dataset's path cannot
            be handed to git lfs as itself.
    """
    pattern = re.compile(leg_filter)
    if mode == Mode.ALL:
        # validate_dataset.py reports success over an empty selection, so a
        # filter that matches nothing has to fail here.
        count = sum(bool(pattern.search(path)) for path in corpus)
        if not count:
            raise ValueError(f"{leg_filter} matches no dataset in the corpus")
        return Leg(lfs_include, leg_filter, Store.HUGGINGFACE, count)
    if mode == Mode.CHANGED:
        share = [path for path in datasets if pattern.search(path)]
        # git lfs splits --include on commas and reads each entry as a pattern,
        # so such a path would not select its own object.
        if unsafe := [path for path in share if _LFS_PATTERN_CHARACTERS.search(path)]:
            raise ValueError(f"cannot pull by exact path: {', '.join(unsafe)}")
        exact = "|".join(re.escape(path) for path in share)
        return Leg(",".join(share), f"^(?:{exact})$", Store.GITHUB, len(share))
    raise ValueError(f"a {mode} run has no legs")


_SUBJECTS = {
    Mode.ALL: "the whole corpus",
    Mode.CHANGED: "the changed datasets",
    Mode.NONE: "nothing",
}


def _write_outputs(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main() -> None:
    """Run the ``scope`` or ``leg`` subcommand and record its step outputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scope_parser = commands.add_parser("scope", help="decide for the event")
    scope_parser.add_argument("--event", required=True)
    scope_parser.add_argument("--fork", default="", help="true for a fork's PR")
    scope_parser.add_argument("--before", default="")
    leg_parser = commands.add_parser("leg", help="narrow one matrix leg")
    leg_parser.add_argument("--mode", type=Mode, required=True)
    leg_parser.add_argument("--datasets", default="[]", help="JSON list of paths")
    leg_parser.add_argument("--filter", required=True)
    leg_parser.add_argument("--lfs_include", required=True)
    for subparser in (scope_parser, leg_parser):
        subparser.add_argument("--github_output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "scope":
        scope = decide(args.event, fork=args.fork == "true", before=args.before)
        print(f"Validating {_SUBJECTS[scope.mode]}: {scope.reason}.")
        for dataset in scope.datasets:
            print(f"  {dataset}")
        outputs = {"mode": scope.mode, "datasets": json.dumps(scope.datasets)}
    else:
        corpus = _git("ls-files", "--", DATA).splitlines()
        share = leg(
            args.mode, json.loads(args.datasets), args.filter, args.lfs_include, corpus
        )
        print(f"This leg validates {share.datasets} datasets from {share.store}.")
        outputs = {
            "lfs_include": share.lfs_include,
            "filter": share.validator_filter,
            "store": share.store,
            "datasets": str(share.datasets),
        }
    _write_outputs(args.github_output, outputs)


if __name__ == "__main__":
    main()
