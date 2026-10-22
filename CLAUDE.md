# CLAUDE.md — ord-data

Project memory for the Open Reaction Database **data** repository. This is
repo-specific context; the global rules in `~/.claude/CLAUDE.md` still apply
(no force-push, commit/PR attribution, preserve bot sections in PR bodies,
Python style, etc.).

## What this repo is

- ORD reaction datasets: one dataset per file under
  `data/<xx>/ord_dataset-<uuid>.parquet`, in a directory named by the first two
  hex chars of the dataset id (256 possible, 48 populated). The file is Parquet,
  not a serialized `Dataset` — reactions are one serialized proto per row, and
  the dataset scalars live in the Parquet footer, so `load_message` will not read
  it. Parquet is the only stored format; submissions may still arrive as
  serialized proto and are converted on the way in.
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

- **`validation.yml`** — validates every dataset, which pulls ~1.2 GB of LFS,
  so the `scope` job (`scripts/validation_scope.py`) runs the shards only when
  a verdict could change: a push to `main` changing `data/**`,
  `validation.yml`, or the **environment**; a PR changing `validation.yml` or
  the environment, or carrying the **`validate-all`** label (every push while
  it stays on); and `workflow_dispatch`. The environment is `.python-version`
  plus ord-schema and its whole dependency closure in `uv.lock` (rdkit,
  protobuf, pyarrow, pandas, … all change verdicts), so a lock bump confined
  to other packages (pygithub, dev tools) does not qualify, and a fork PR's
  environment never does (it is validated in the base branch's). There is
  **no weekly sweep**: nothing else makes a verdict go stale. Every PR runs
  `check_layout` and `scope`, which read no LFS. A `labeled` event for any
  other label gets a concurrency group of its own so it cannot cancel a real
  run; the label is spelled in both the concurrency key and the script, and a
  test fails if they drift. `validate_parquet` can skip, so it must never be a
  required check. 4-shard `validate_parquet`
  matrix keyed on the first hex digit of the `data/<xx>/` directory (`0-3`,
  `4-7`, `8-b`, `c-f`), so a new dataset needs no matrix edit; each shard
  sparse-pulls only its objects from GitHub. `matrix.filter` is the
  `validate_dataset.py` regex and `lfs_include` the equivalent LFS glob, carried
  separately because one is a regex and the other a glob.
  **`validate_dataset.py` exits 0 when its pattern matches nothing**, so
  coverage is asserted rather than assumed by
  three guards: `check_layout` fails if anything under `data/` is not a
  `.parquet` exactly one directory deep, and separately re-reads the `filter:`
  values out of this workflow to assert they match every dataset **exactly
  once** (catching a gap or an overlap between shards); each shard then fails if
  its own filter selects no files (catching a shard that matches nothing while
  its siblings still match plenty). Do not remove them — without them a stale
  filter or a stray suffix is published unvalidated behind a green check. The job passes `--validate_ids`,
  matching what `process_dataset.py`
  checks at submission; it is opt-in upstream because ids are assigned *during*
  submission, so a draft does not have them yet. Uses
  `concurrency: cancel-in-progress` — pushing again cancels the running matrix.
  Its environment is a separate sparse checkout (`pipeline/`, just
  `.python-version` / `pyproject.toml` / `uv.lock`) taken from the **base branch
  on fork PRs** and from `github.sha` otherwise — deliberately narrower than
  `submission.yml`'s guard. Pinning it to the base branch on every PR takes the
  workflow file from the merge ref and its environment from `main`, so a PR that
  bumps `uv.lock` **and** changes the validator's command line cannot test
  itself.
- **`submission.yml`** — per-PR. `process_submission` sparse-pulls only the
  changed datasets, gated on `NUM_CHANGED_FILES`. Exactly one of `Validate
  submission` / `Update submission` runs when a dataset changed: both gate on
  the `mode` step's one output, so give neither a condition of its own.
  Validate runs on fork PRs, on the re-run from the workflow's own push, and on
  PRs labeled `skip-update-submission` (the label skips the rewrite, not the
  check); every other PR runs Update. The pipeline it runs is checked out from
  the **base branch** into `pipeline/`, never from the PR: on a fork,
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

- The USPTO grants are 41 datasets, `uspto-grants-1976` … `uspto-grants-2016`,
  one per grant year (7–91 MB each); `retired_datasets.csv` maps the retired
  single-file `uspto-grants` (`ord_dataset-1158…`) to them. The Hugging Face
  `uspto-grants` config lists them via `USPTO_GRANTS_IDS` in
  `scripts/upload_to_huggingface.py`, so a change to the set of years goes there.
- The reactions-count badge and its `count_reactions` job were intentionally
  removed (low value, and it added a bot "Update badges" commit to every PR).
  Don't reintroduce.
- To confirm a store holds every LFS object before relying on it for reads, use
  the `verify-lfs-store` skill (`.claude/skills/verify-lfs-store`). It checks the
  HF mirror by default and GitHub with `--store github`, which is how a tag whose
  objects a later commit deleted gets confirmed to still resolve.
