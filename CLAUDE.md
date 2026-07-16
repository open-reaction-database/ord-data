# CLAUDE.md — ord-data

Project memory for the Open Reaction Database **data** repository. This is
repo-specific context; the global rules in `~/.claude/CLAUDE.md` still apply
(no force-push, commit/PR attribution, preserve bot sections in PR bodies,
Python style, etc.).

## What this repo is

- ORD reaction datasets: one `Dataset` proto per file under
  `data/<xx>/ord_dataset-<uuid>.{pb.gz,parquet}`, sharded into 256 directories
  by the first two hex chars of the dataset id.
- Large dataset files are stored with **Git LFS**. The schema and all tooling
  (`process_dataset.py`, `validate_dataset.py`, the badge generator) live in the
  separate `Open-Reaction-Database/ord-schema` repo, pinned in every workflow via
  the `ORD_SCHEMA_TAG` env var.

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
   plain file, not LFS), and opens a PR to a **non-`main`** branch.
2. `submission.yml` validates it (`process_dataset.py`; the `Validate submission`
   step for fork PRs).
3. A maintainer merges into the non-`main` branch; the non-fork run's `Update
   submission` step runs `process_dataset.py --update --cleanup`, which assigns
   ids and **moves the file into `data/`** (where it becomes an LFS object), then
   commits and pushes.
4. A maintainer PRs the non-`main` branch into `main`. On push to `main`,
   `validation.yml` validates and `huggingface_mirror.yml` mirrors the new
   objects to HF.

`process_dataset.py` reads only the files in `changed_data_files.txt`; base
revisions of modified files are smudged on demand via `lfs.url`. It never scans
the whole dataset, so submission CI only needs the **changed** objects.

## Workflows (`.github/workflows`)

- **`validation.yml`** — validates every dataset. Triggers on push to `main` and
  on PRs that touch `validation.yml`. 11-shard matrix: 9 `validate_pb` shards by
  `data/<hex><hex>` prefix + 2 `validate_parquet` (uspto / other). Each shard
  sparse-pulls only its objects from GitHub. For pb shards, `matrix.filter`
  doubles as both the `validate_dataset.py` regex and the LFS `--include` glob
  (parquet needs a separate `lfs_include` because its filter is a lookahead
  regex). Uses `concurrency: cancel-in-progress` — pushing again cancels the
  running matrix.
- **`submission.yml`** — per-PR. `process_submission` sparse-pulls only the
  changed datasets, gated on `NUM_CHANGED_FILES`. Fork PRs run validate-only;
  non-fork PRs run the `Update submission` step (skippable via the
  `skip-update-submission` label).
- **`huggingface_mirror.yml`** — mirrors `data/**` + docs to HF on push to
  `main` (dry-run on PRs) via `scripts/upload_to_huggingface.py` + `HF_TOKEN`.

## Bandwidth / billing

LFS download bandwidth was ~87% clones/forks. Inspect usage with:
`gh api '/orgs/open-reaction-database/settings/billing/usage?year=YYYY&month=M'`
and filter `product=="git_lfs"` (enhanced-billing endpoint; needs an org-billing
token scope).

## Gotchas

- The USPTO grants parquet (`data/11/ord_dataset-1158…parquet`) is ~1 GB — the
  long pole in validation, and over GitHub's 100 MB non-LFS limit, so it must
  stay LFS.
- The reactions-count badge and its `count_reactions` job were intentionally
  removed (low value, and it added a bot "Update badges" commit to every PR).
  Don't reintroduce.
- To confirm the HF mirror holds every LFS object before relying on it for
  reads, use the `verify-hf-mirror` skill (`.claude/skills/verify-hf-mirror`).
