# CLAUDE.md — ord-data

Project memory for the Open Reaction Database **data** repository. This is
repo-specific context; the global rules in `~/.claude/CLAUDE.md` still apply
(no force-push, commit/PR attribution, preserve bot sections in PR bodies,
Python style, etc.).

## What this repo is

- ORD reaction datasets: one `Dataset` proto per file under
  `data/<xx>/ord_dataset-<uuid>.{pb.gz,parquet}`, sharded into 256 directories
  by the first two hex chars of the dataset id.
- Large dataset files are stored with **Git LFS**. The schema library lives in
  `Open-Reaction-Database/ord-schema` and is installed from PyPI by the
  `pipeline` dependency group, so **`uv.lock` is the only place its version is
  written down** — `validation.yml` runs `validate_dataset.py` out of the
  installed wheel (`python -m ord_schema.scripts.validate_dataset`) rather than
  pinning a tag of its own, so submission-time and merge-time validation cannot
  disagree. `process_dataset.py` lives here in `scripts/`.

## Local checks

`uv run pytest -n auto` · `uv run ruff check .` · `uv run ty check` ·
`uv run pre-commit run --all-files`. Python is pinned to 3.11 by
`.python-version`; `uv sync --locked` honors it.

## Git LFS ↔ Hugging Face mirror — read before touching LFS or workflows

The full design is in the README ("Git LFS and the Hugging Face mirror") and the
`.lfsconfig` header. The short version: **reads go to the HF mirror, writes go
to GitHub, GitHub is the source of truth.** HF is a read replica synced by
`huggingface_mirror.yml` on every push to `main`.

Invariants — easy to break, so check these when editing any workflow or LFS config:

- **CI and the mirror must read LFS from GitHub, never HF.** Before any
  `git lfs pull`, set
  `git config lfs.url "https://github.com/${GITHUB_REPOSITORY}.git/info/lfs"`.
  Freshly pushed objects are not on HF until the post-merge mirror job runs, so
  reading from HF in CI would race or 404.
- **Never commit an `lfs.pushurl` to `.lfsconfig`.** A fixed HTTPS pushurl breaks
  SSH pushers; a fixed SSH pushurl breaks CI. Writers set their own endpoint (CI
  reuses the `lfs.url` override above; a fork editing an existing `data/` object
  sets a local `git config lfs.pushurl …`).
- **LFS is scoped to `data/`** in `.gitattributes`. Keep it that way: a new
  submission staged at the repo root must stay a plain (non-LFS) file so forks
  can push it without any LFS setup.
- **Don't hand-edit the HF dataset.** It is regenerated from GitHub by the mirror;
  if it drifts, re-run the mirror rather than editing HF directly.

## Data submission flow

1. An external contributor forks, adds a dataset file at the **repo root** (a
   plain file, not LFS), and opens a PR against **`main`**. Accepted suffixes:
   `.pb`, `.binpb`, `.pbtxt`, `.txtpb` (each optionally `.gz`), `.parquet`.
2. `retarget_submission.yml` moves it onto a `#<number>` branch in this repo and
   comments to say so. Contributors are not expected to know the convention.
3. `submission.yml` validates it (`process_dataset.py`; the `Validate submission`
   step for fork PRs). Every invalid file is reported, not just the first.
4. A maintainer merges into the `#<number>` branch; the non-fork run's `Update
   submission` step runs `process_dataset.py --update --cleanup`, which assigns
   ids and **moves the file into `data/`** as **Parquet** (where it becomes an
   LFS object), then commits and pushes. An edit to a dataset already in the
   repository keeps whatever format it has; only new submissions are written in
   the standard one.
5. A maintainer PRs the `#<number>` branch into `main`. On push to `main`,
   `validation.yml` validates and `huggingface_mirror.yml` mirrors the new
   objects to HF.

`process_dataset.py` reads only the files in `changed_data_files.txt`; base
revisions of modified files are smudged on demand via `lfs.url`. It never scans
the whole dataset, so submission CI only needs the **changed** objects.

## Workflows (`.github/workflows`)

- **`validation.yml`** — validates every dataset. Triggers on push to `main`
  **only under `data/**`**, on PRs that touch `validation.yml`, and weekly
  (Mon 07:00 UTC) for the full sweep, so a workflow-only merge does not pull
  2.4 GB of LFS to validate data nobody changed. 11-shard matrix: 9 `validate_pb` shards by
  `data/<hex><hex>` prefix + 2 `validate_parquet` (uspto / other). Each shard
  sparse-pulls only its objects from GitHub. For pb shards, `matrix.filter`
  doubles as both the `validate_dataset.py` regex and the LFS `--include` glob
  (parquet needs a separate `lfs_include` because its filter is a lookahead
  regex). Uses `concurrency: cancel-in-progress` — pushing again cancels the
  running matrix. Like `submission.yml`, it builds its environment from a
  repo-owned ref (`pipeline/`, sparse-checked-out to just `.python-version` /
  `pyproject.toml` / `uv.lock`) because a `pull_request` run checks out the
  contributor-controlled merge ref.
- **`submission.yml`** — per-PR. `process_submission` sparse-pulls only the
  changed datasets, gated on `NUM_CHANGED_FILES`. Fork PRs run validate-only;
  non-fork PRs run the `Update submission` step (skippable via the
  `skip-update-submission` label). The pipeline it runs is checked out from the
  **base branch** into `pipeline/`, never from the PR: on a fork,
  `process_dataset.py` and `uv.lock` are contributor-controlled.
- **`retarget_submission.yml`** — `pull_request_target`, API-only, checks out
  nothing. Creates the `#<number>` branch, brings it up to date with `main`
  (fast-forward, else merge), and repoints the PR.
- **`huggingface_mirror.yml`** — mirrors `data/**` + docs to HF on push to
  `main` (dry-run on PRs) via `scripts/upload_to_huggingface.py` + `HF_TOKEN`.

## Bandwidth / billing

LFS download bandwidth was ~87% clones/forks. Inspect usage with:
`gh api '/orgs/open-reaction-database/settings/billing/usage?year=YYYY&month=M'`
and filter `product=="git_lfs"` (enhanced-billing endpoint; needs an org-billing
token scope).

## Gotchas — CI

- Required checks on `main` are `process_submission` and `retarget`. Neither
  they nor anything they `needs` may carry a job-level `if:`: a **skipped**
  required check blocks a merge exactly like a failing one, and reports over
  whatever passed before.
- Events made with `GITHUB_TOKEN` start no workflow run. That is why the
  submission push and the retarget repoint use a GitHub App
  (`SUBMISSION_APP_ID` variable, `SUBMISSION_APP_PRIVATE_KEY` secret) — and why
  a workflow's own commit used to arrive with no CI at all.
- `pull_request_target` workflows run the copy on `main`, so a change to when
  one reports takes effect only **after** merge, never in the PR that makes it.
  Requiring such a check before its enabling change lands deadlocks every PR.
- Take a PR's changed files from `gh api …/pulls/N/files`, never
  `git diff upstream/main`: a `#<number>` branch that has fallen behind makes
  every intervening merge look like a submitted file. The endpoint truncates at
  3000, so compare against the PR's own `changed_files` count.
- The repo squash-merges, so a stacked PR goes `CONFLICTING` when its parent
  lands even though the content is identical. Verify the squash tree matches the
  old branch tip, rebase onto `main` in a scratch worktree, then apply that tree
  as a merge — never force-push.
- `DATASET_FILE_PATTERN` is duplicated in `submission.yml` and
  `retarget_submission.yml` on purpose (a `pull_request` run checks out the merge
  ref, so a shared file would be the fork's copy); `tests.yml` fails if they
  drift.

## Gotchas

- The USPTO grants parquet (`data/11/ord_dataset-1158…parquet`) is ~1 GB — the
  long pole in validation, and over GitHub's 100 MB non-LFS limit, so it must
  stay LFS.
- The reactions-count badge and its `count_reactions` job were intentionally
  removed (low value, and it added a bot "Update badges" commit to every PR).
  Don't reintroduce.
- To confirm the HF mirror holds every LFS object before relying on it for
  reads, use the `verify-hf-mirror` skill (`.claude/skills/verify-hf-mirror`).
