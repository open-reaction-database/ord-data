---
name: verify-lfs-store
description: Verify that an LFS store — the Hugging Face mirror by default, or GitHub with `--store github` — actually serves every object referenced by a git ref. Use before relying on HF for LFS reads (e.g. before/after changing `.lfsconfig`, after a large merge, or to audit mirror completeness), and to confirm the GitHub fallback still resolves objects a later commit deleted. Confirms redirected clones won't 404.
---

# Verify LFS store completeness

The repo redirects Git LFS reads to the Hugging Face mirror (see the README and
`.lfsconfig`). A redirected clone breaks if HF is missing any object the ref
references, so before trusting HF for reads — or after a merge that adds objects
— confirm HF's LFS store serves every object on the ref. The same check runs
against GitHub, which is where writers push and what CI and forks read.

## How it works

`verify_lfs_store.py` reads the LFS pointers for a git ref from the local clone,
then asks an LFS **batch API** (`operation=download`) whether each `oid`/`size`
is present. Anything that comes back with an `error` (typically 404) is missing
from that store. It does **not** download object bytes (the batch API just
returns presence + presigned URLs), so it is cheap, and an anonymous
(token-free) query proves public/fork clones can resolve.

`--store` picks which store to ask: `hf` (the default) is the endpoint
`.lfsconfig` configures, so a pass means a redirected clone resolves; `github` is
the source of truth writers push to, and the fallback CI and forks override
`lfs.url` to. Both endpoints are this repository's, taken from constants —
running the script inside a fork still audits `open-reaction-database/ord-data`,
not the fork's own store.

## Usage

```bash
# Default: check origin/main (what the mirror tracks).
python .claude/skills/verify-lfs-store/verify_lfs_store.py

# Check a specific ref (e.g. a feature branch before merging it).
python .claude/skills/verify-lfs-store/verify_lfs_store.py origin/main

# Ask GitHub instead of HF (e.g. a tag whose objects a later commit deleted).
python .claude/skills/verify-lfs-store/verify_lfs_store.py v0.1.1 --store github
```

- Fetch the ref first so the local pointers are current: `git fetch origin
  <branch>`, or `git fetch origin tag <name>` for a tag — fetching a tag by
  bare name writes `FETCH_HEAD` and no local tag, and the script then fails in
  `git lfs ls-files`.
- Exit 0 and "All N objects present on <store> LFS." means that store resolves
  every object on the ref.
- Exit 1 lists the missing `oid`/path pairs. Against `hf`, a feature branch
  legitimately reports its not-yet-merged objects as missing — those reach HF
  only after they merge to `main` and the mirror job runs, so re-check against
  `origin/main` after the merge. Against `github` there is no such lag: a
  pushed branch's objects are there immediately, so a miss means they were
  never pushed.
- Exit 2 means a pointer did not parse; exit 3 means the store did not answer
  (a timeout, or an HTTP status such as GitHub's 403 when an LFS quota is
  exhausted). Neither is evidence that the objects are present.

## Notes

- Both constants are final URLs that answer directly, so the script does not
  follow redirects. A redirect means an endpoint moved, which is worth failing
  on rather than chasing.
- No `HF_TOKEN` is needed for a public dataset; the anonymous download batch is
  what a public clone uses. GitHub's batch endpoint answers anonymously for a
  public repository too, so neither store needs credentials.
- Deleting a file in a commit does not by itself drop its object from either
  store, so a tag predating a deletion can still resolve — confirmed for both
  stores at `v0.1.1` after the `.pb.gz` removal. Treat it as an observation to
  re-check, not a guarantee: on GitHub the object stays in the repository's LFS
  store, while on HF it survives because the mirror deletes with
  `CommitOperationDelete`, leaving the blob reachable from history. Rebuilding
  the mirror from current `data/` would not carry history-only objects.
