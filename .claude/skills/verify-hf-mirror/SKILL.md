---
name: verify-hf-mirror
description: Verify the Hugging Face mirror's Git LFS store actually serves every object referenced by a git ref. Use before relying on HF for LFS reads (e.g. before/after changing `.lfsconfig`, after a large merge, or to audit mirror completeness). Confirms redirected clones won't 404.
---

# Verify HF mirror completeness

The repo redirects Git LFS reads to the Hugging Face mirror (see the README and
`.lfsconfig`). A redirected clone breaks if HF is missing any object the ref
references, so before trusting HF for reads — or after a merge that adds objects
— confirm HF's LFS store serves every object on the ref.

## How it works

`verify_hf_lfs.py` reads the LFS pointers for a git ref from the local clone,
then asks HF's LFS **batch API** (`operation=download`) whether each `oid`/`size`
is present. Anything that comes back with an `error` (typically 404) is missing
from HF. It does **not** download object bytes (the batch API just returns
presence + presigned URLs), so it is cheap, and an anonymous (token-free) query
proves public/fork clones can resolve.

## Usage

```bash
# Default: check origin/main (what the mirror tracks).
python .claude/skills/verify-hf-mirror/verify_hf_lfs.py

# Check a specific ref (e.g. a feature branch before merging it).
python .claude/skills/verify-hf-mirror/verify_hf_lfs.py origin/main
```

- Fetch the ref first (`git fetch origin <ref>`) so the local pointers are current.
- Exit 0 and "All N objects present on HF LFS." means redirected clones resolve.
- Exit 1 lists the missing `oid`/path pairs. A feature branch will legitimately
  report its not-yet-merged objects as missing — those reach HF only after they
  merge to `main` and the mirror job runs. Re-check against `origin/main` after
  the merge.

## Notes

- The HF batch endpoint 307-redirects `hf.co` → `huggingface.co`; the script
  follows that for POST manually (urllib won't).
- No `HF_TOKEN` is needed for a public dataset; the anonymous download batch is
  what a public clone uses.
