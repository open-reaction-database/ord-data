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

"""Decide whether a Validation workflow run validates the whole corpus.

Validating the corpus pulls all of it from GitHub LFS, so a run does so only
when its event could change a verdict:

* a manual dispatch, always;
* a push to main that changes data/, validation.yml, or the environment;
* a pull request that changes validation.yml or the environment, or that
  carries the validate-all label. A pull request's own datasets are
  submission.yml's to validate.

The environment is the interpreter named by .python-version plus ord-schema and
every package it depends on, as uv.lock resolves them. A lock change confined to
other packages leaves it alone. A fork's pull request is validated in its base
branch's environment, so the fork's own uv.lock and .python-version are not
under test.

Writes ``validate_corpus=true`` or ``validate_corpus=false`` to the step output
file. Runs on the system interpreter before anything is installed, so it uses
the standard library only.
"""

import argparse
import itertools
import json
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

# validation.yml's concurrency group names this label too, since it cannot read
# it from here; validation_scope_test.py checks that the two agree.
LABEL = "validate-all"
WORKFLOW = ".github/workflows/validation.yml"
INTERPRETER = ".python-version"
LOCK = "uv.lock"
VALIDATOR = "ord-schema"


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


def decide(
    event: str,
    *,
    action: str = "",
    added_label: str = "",
    labels: Sequence[str] = (),
    fork: bool = False,
    before: str = "",
) -> str | None:
    """Return why this run validates the corpus, or None if it should not.

    Args:
        event: The triggering event: push, pull_request, or workflow_dispatch.
        action: A pull_request event's activity type, such as labeled.
        added_label: The label a labeled event added.
        labels: The labels the pull request carries.
        fork: Whether the pull request comes from a fork.
        before: The commit a push moved main from.

    Returns:
        A reason to log, or None when nothing the event changed can alter a
        verdict.

    Raises:
        ValueError: If ``event`` is not one the workflow triggers on.
    """
    if event == "workflow_dispatch":
        return "manual dispatch"
    if event == "pull_request":
        if action == "labeled":
            # This commit's opening or push already got a run that decided on
            # its own; any other label leaves that decision standing.
            return f"{LABEL} label added" if added_label == LABEL else None
        if LABEL in labels:
            return f"pull request carries the {LABEL} label"
        # HEAD is the merge commit, and its first parent the base it was made on.
        base, watched, environment_under_test = "HEAD^1", [WORKFLOW], not fork
    elif event == "push":
        _ensure_commit(before)
        base, watched, environment_under_test = before, [WORKFLOW, "data"], True
    else:
        raise ValueError(f"no rule for {event} events")
    if environment_under_test:
        watched.append(INTERPRETER)
    changed = _git("diff", "--name-only", base, "HEAD", "--", *watched).splitlines()
    if changed:
        return f"changes {', '.join(changed)}"
    if environment_under_test:
        difference = environment(_git("show", f"{base}:{LOCK}")) ^ environment(
            _git("show", f"HEAD:{LOCK}")
        )
        if difference:
            names = sorted({name for name, _, _ in difference})
            return f"changes {', '.join(names)} in {LOCK}"
    return None


def main() -> None:
    """Decide for the triggering event and record the verdict as a step output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--action", default="")
    parser.add_argument("--added_label", default="")
    parser.add_argument("--labels", default="[]", help="JSON list of label names")
    parser.add_argument("--fork", default="", help="true for a fork's pull request")
    parser.add_argument("--before", default="")
    parser.add_argument("--github_output", type=Path, required=True)
    args = parser.parse_args()
    reason = decide(
        args.event,
        action=args.action,
        added_label=args.added_label,
        labels=json.loads(args.labels),
        fork=args.fork == "true",
        before=args.before,
    )
    if reason:
        print(f"Validating the corpus: {reason}.")
    else:
        print("Not validating the corpus: nothing this run changed alters a verdict.")
    with args.github_output.open("a") as output:
        output.write(f"validate_corpus={'true' if reason else 'false'}\n")


if __name__ == "__main__":
    main()
