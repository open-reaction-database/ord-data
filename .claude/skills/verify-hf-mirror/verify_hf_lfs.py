"""Check that HF's LFS store serves every object referenced by a git ref.

Reads Git LFS pointers from a git ref in the local clone, then asks the Hugging
Face LFS batch API (operation=download) whether each oid/size is present. Objects
returned with an ``error`` (typically 404) are missing from HF, which would break
a clone that resolves LFS reads through the mirror (see ``.lfsconfig``).

Usage:
    python verify_hf_lfs.py [GIT_REF]   # GIT_REF defaults to origin/main
"""

import json
import subprocess
import sys
import urllib.error
import urllib.request

REF = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
HF_BATCH = (
    "https://hf.co/datasets/open-reaction-database/ord-data.git/info/lfs/objects/batch"
)
CHUNK = 100


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


def check(objects: list[tuple[str, int, str]]) -> list[tuple[str, str]]:
    """Return the list of (oid, path) that HF reports as missing."""
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
        url = HF_BATCH
        for _ in range(5):  # follow 307/308 redirects, preserving POST+body
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Accept": "application/vnd.git-lfs+json",
                    "Content-Type": "application/vnd.git-lfs+json",
                },
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (307, 308) and exc.headers.get("Location"):
                    url = exc.headers["Location"]
                    continue
                raise
        else:
            raise RuntimeError("too many redirects")
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
        # present, and this audit exists to decide whether the mirror can be
        # trusted for reads. Silence is not evidence, so count it as missing.
        missing.extend(
            (oid, path) for oid, size, path in chunk if (oid, size) not in returned
        )
        print(f"  checked {min(start + CHUNK, len(objects))}/{len(objects)}")
    return missing


def main() -> None:
    """Reports any LFS object at ``REF`` that the Hugging Face mirror is missing."""
    objects = lfs_pointers(REF)
    print(f"{REF}: {len(objects)} LFS objects")
    # Stop rather than warn: a pointer with no oid or size would be sent to the
    # batch API as null and come back unanswered, and the run would end up
    # reporting a complete mirror having never checked that path.
    unparsed = [o[2] for o in objects if not o[0] or not o[1]]
    if unparsed:
        print(f"ERROR: {len(unparsed)} pointers did not parse:", file=sys.stderr)
        for path in unparsed:
            print(f"  {path}", file=sys.stderr)
        sys.exit(2)
    missing = check(objects)
    print()
    if missing:
        print(f"MISSING from HF: {len(missing)}")
        for oid, path in missing:
            print(f"  {oid[:12]}  {path}")
        sys.exit(1)
    print(f"All {len(objects)} objects present on HF LFS.")


if __name__ == "__main__":
    main()
