---
name: verify-lfs-store
description: Verify that an LFS store — the Hugging Face mirror by default, or GitHub with `--store github` — actually serves every object referenced by a git ref. Use before relying on HF for LFS reads (e.g. before/after changing `.lfsconfig`, after a large merge, or to audit mirror completeness), and to confirm the GitHub fallback still resolves objects a later commit deleted. Confirms redirected clones won't 404.
---

# Verify LFS store completeness

The repo redirects Git LFS reads to the Hugging Face mirror (see the README and
`.lfsconfig`). A redirected clone breaks if HF is missing any object the ref
references, so before trusting HF for reads — or after a merge that adds objects
— confirm HF's LFS store serves every object on the ref.

## How it works

`verify_lfs_store.py` reads the LFS pointers for a git ref from the local clone,
then asks an LFS **batch API** (`operation=download`) whether each `oid`/`size`
is present. Anything that comes back with an `error` (typically 404) is missing
from that store. It does **not** download object bytes (the batch API just
returns presence + presigned URLs), so it is cheap, and an anonymous
(token-free) query proves public/fork clones can resolve.

`--store` picks which store to ask: `hf` (the default) is where `.lfsconfig`
sends reads; `github` is the source of truth writers push to, and the endpoint a
fork or CI overrides `lfs.url` to.

## Usage

```bash
# Default: check origin/main (what the mirror tracks).
python .claude/skills/verify-lfs-store/verify_lfs_store.py

# Check a specific ref (e.g. a feature branch before merging it).
python .claude/skills/verify-lfs-store/verify_lfs_store.py origin/main

# Ask GitHub instead of HF (e.g. a tag whose objects a later commit deleted).
python .claude/skills/verify-lfs-store/verify_lfs_store.py v0.1.1 --store github
```

- Fetch the ref first (`git fetch origin <ref>`) so the local pointers are current.
- Exit 0 and "All N objects present on hf LFS." means redirected clones resolve.
- Exit 1 lists the missing `oid`/path pairs. A feature branch will legitimately
  report its not-yet-merged objects as missing — those reach HF only after they
  merge to `main` and the mirror job runs. Re-check against `origin/main` after
  the merge.

## Notes

- The HF batch endpoint 307-redirects `hf.co` → `huggingface.co`; the script
  follows that for POST manually (urllib won't).
- No `HF_TOKEN` is needed for a public dataset; the anonymous download batch is
  what a public clone uses. GitHub's batch endpoint answers anonymously for a
  public repository too, so neither store needs credentials.
- Deleting a file in a commit does not drop its object from either store, so a
  tag predating a deletion still resolves. `--store` is how that gets confirmed
  rather than assumed.
