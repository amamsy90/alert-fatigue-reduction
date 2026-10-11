"""Download RCAEval RE2 and RE3 (logs and metrics) and check every file's SHA-256.

RCAEval (Pham et al., 2025; https://huggingface.co/datasets/phamquiluan/RCAEval,
MIT licence) injects faults into three microservice applications (Online
Boutique, Sock Shop, Train Ticket) and records metrics, logs and traces:

  RE2   270 cases: CPU, memory, disk, network delay, packet loss, socket faults
  RE3    90 cases: code-level faults (F1-F5) that show up in logs and traces

Each case is a separate recording of 24-30 minutes, half before and half
after the injection. Only `logs.parquet` and `metrics.parquet` are kept
(traces are not used, which saves about 1 GB). The SHA-256 and size of every
file are pinned in tools/rcaeval_re23_sha256.txt (taken from the dataset's
Git LFS pointers and checked against the downloaded bytes); a mismatch stops
the run. About 570 MB in all.

    python tools/fetch_rcaeval_re23.py               # RE2 and RE3
    python tools/fetch_rcaeval_re23.py --suite RE3   # RE3 only (about 40 MB)
    python tools/fetch_rcaeval_re23.py --check       # verify files already in place

Writes app/data/rcaeval_re23/cases.parquet and app/data/rcaeval_re23/<case>/*.parquet
(git-ignored).
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import urlopen

HF = "https://huggingface.co/datasets/phamquiluan/RCAEval/resolve/main"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "data" / "rcaeval_re23"
MANIFEST = Path(__file__).resolve().parent / "rcaeval_re23_sha256.txt"
CASES = ("c49a288920dbba2e8e724679a14636d5c7eb2b45426bba14007ef79a6c0ab1bb", 29500)


def expected(suites: tuple[str, ...]) -> dict[str, tuple[str, int]]:
    """Relative path -> (SHA-256, size) for the chosen suites."""
    out: dict[str, tuple[str, int]] = {"cases.parquet": CASES}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sha, size, rel = line.split()
        if rel[:3].upper() in suites:  # case folders start with re2 / re3
            out[rel] = (sha, int(size))
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _ok(path: Path, want: tuple[str, int]) -> bool:
    return path.exists() and path.stat().st_size == want[1] and _sha256(path) == want[0]


def _fetch_one(rel: str, want: tuple[str, int]) -> None:
    dest = OUT / rel
    if _ok(dest, want):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urlopen(f"{HF}/{rel}", timeout=300) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    if not _ok(tmp, want):
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"SHA-256 mismatch for {rel}: the upstream file changed; results may differ")
    tmp.replace(dest)


def fetch(files: dict[str, tuple[str, int]], workers: int = 8) -> None:
    """Download what is missing, a few files at a time (each file is small)."""
    with ThreadPoolExecutor(workers) as pool:
        jobs = [pool.submit(_fetch_one, rel, want) for rel, want in sorted(files.items())]
        for i, job in enumerate(as_completed(jobs), 1):
            job.result()
            if i % 100 == 0:
                print(f"  {i}/{len(files)} files")


def check(files: dict[str, tuple[str, int]]) -> int:
    bad = [rel for rel, want in files.items() if not _ok(OUT / rel, want)]
    for rel in bad[:10]:
        print(f"missing or different: {rel}")
    print("RCAEval RE2/RE3 files OK" if not bad else f"{len(bad)} file(s) missing or different")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", choices=["RE2", "RE3"], action="append", help="only this suite (repeatable)")
    ap.add_argument("--check", action="store_true", help="only verify files already in place")
    args = ap.parse_args()
    files = expected(tuple(args.suite or ("RE2", "RE3")))
    if not args.check:
        print(f"{len(files)} files, {sum(s for _, s in files.values()) / 1e6:.0f} MB")
        fetch(files)
    sys.exit(check(files))
