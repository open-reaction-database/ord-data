"""Check that an LFS store serves every object referenced by a git ref.

Reads Git LFS pointers from a git ref in the local clone, then asks an LFS batch
API (operation=download) whether each oid/size is present. Objects returned with
an ``error`` (typically 404) are missing from that store. For the Hugging Face
mirror that would break a clone resolving its reads there (see ``.lfsconfig``);
for GitHub it would break the fallback those clones override to.

Usage:
    python verify_lfs_store.py [GIT_REF] [--store {github,hf}]
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

# The hf entry is the URL .lfsconfig configures, so the audit exercises the
# endpoint a redirected clone actually reads from. Both are final URLs; a
# redirect from either is a change worth failing on rather than following.
STORES = {
    "hf": (
        "https://huggingface.co/datasets/open-reaction-database"
        "/ord-data.git/info/lfs/objects/batch"
    ),
    "github": (
        "https://github.com/open-reaction-database/ord-data.git/info/lfs/objects/batch"
    ),
}
CHUNK = 100
TIMEOUT = 60


def lfs_pointers(ref: str) -> list[tuple[str, int, str]]:
    """Yield (oid, size, path) for every LFS object referenced at ``ref``."""
    paths = subprocess.run(
        ["git", "lfs", "ls-files", "-n", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    # Batch-read every pointer blob via `git cat-file --batch`.
    requests = "".join(f"{ref}:{p}\n" for p in paths)
    out = subprocess.run(
        ["git", "cat-file", "--batch"],
        input=requests,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    i = 0
    results = []
    for path in paths:
        # Each record: "<sha> blob <size>\n<contents>\n".
        header_end = out.index("\n", i)
        _, _, blob_size = out[i:header_end].split()
        body = out[header_end + 1 : header_end + 1 + int(blob_size)]
        i = header_end + 1 + int(blob_size) + 1  # skip trailing newline
        oid = size = None
        for line in body.splitlines():
            if line.startswith("oid sha256:"):
                oid = line.split("sha256:", 1)[1].strip()
            elif line.startswith("size "):
                size = int(line.split(None, 1)[1])
        results.append((oid, size, path))
    return results


def check(objects: list[tuple[str, int, str]], endpoint: str) -> list[tuple[str, str]]:
    """Returns the (oid, path) pairs the store at ``endpoint`` does not serve.

    Args:
        objects: (oid, size, path) triples to look up.
        endpoint: LFS batch API URL to query.

    Returns:
        One (oid, path) pair per object the store reports as missing.
    """
    missing = []
    for start in range(0, len(objects), CHUNK):
        chunk = objects[start : start + CHUNK]
        payload = json.dumps(
            {
                "operation": "download",
                "transfers": ["basic"],
                "objects": [{"oid": o, "size": s} for o, s, _ in chunk],
            }
        ).encode()
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Accept": "application/vnd.git-lfs+json",
                "Content-Type": "application/vnd.git-lfs+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, TimeoutError) as exc:
            # A store that refuses the query has not been shown to hold
            # anything. Exiting 3 keeps that distinct from 1 (objects
            # missing), so a saved log cannot read as a clean audit.
            reason = getattr(exc, "code", None) or getattr(exc, "reason", exc)
            print(
                f"\nERROR: {endpoint} did not answer ({reason}); "
                f"{start} of {len(objects)} objects checked.",
                file=sys.stderr,
            )
            sys.exit(3)
        by_oid = {(o, s): p for o, s, p in chunk}
        returned = {
            (obj.get("oid"), obj.get("size")) for obj in data.get("objects", [])
        }
        missing.extend(
            (obj["oid"], by_oid.get((obj["oid"], obj.get("size")), "?"))
            for obj in data.get("objects", [])
            if "error" in obj
        )
        # An object the batch response simply left out has not been shown to be
        # present, and this audit exists to decide whether the store can be
        # trusted for reads. Silence is not evidence, so count it as missing.
        missing.extend(
            (oid, path) for oid, size, path in chunk if (oid, size) not in returned
        )
        print(f"  checked {min(start + CHUNK, len(objects))}/{len(objects)}")
    return missing


def main() -> None:
    """Reports any LFS object at the requested ref that the chosen store misses."""
    parser = argparse.ArgumentParser(
        description=next(iter((__doc__ or "").splitlines()), None)
    )
    parser.add_argument(
        "ref",
        nargs="?",
        default="origin/main",
        help="Git ref to check (default: %(default)s)",
    )
    parser.add_argument(
        "--store",
        choices=sorted(STORES),
        default="hf",
        help=(
            "LFS store to query; hf is the endpoint .lfsconfig configures "
            "(default: %(default)s)"
        ),
    )
    args = parser.parse_args()
    objects = lfs_pointers(args.ref)
    print(f"{args.ref}: {len(objects)} LFS objects")
    # Stop rather than warn: a pointer with no oid or size would be sent to the
    # batch API as null and come back unanswered, and the run would end up
    # reporting a complete store having never checked that path.
    unparsed = [o[2] for o in objects if o[0] is None or o[1] is None]
    if unparsed:
        print(f"ERROR: {len(unparsed)} pointers did not parse:", file=sys.stderr)
        for path in unparsed:
            print(f"  {path}", file=sys.stderr)
        sys.exit(2)
    missing = check(objects, STORES[args.store])
    print()
    if missing:
        print(f"MISSING from {args.store}: {len(missing)}")
        for oid, path in missing:
            print(f"  {oid[:12]}  {path}")
        sys.exit(1)
    print(f"All {len(objects)} objects present on {args.store} LFS.")


if __name__ == "__main__":
    main()
