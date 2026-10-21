"""Download RCAEval RE2 and RE3 traces. Logs and metrics stay on the pinned fetch.

``tools/fetch_rcaeval_re23.py`` stores ``logs.parquet`` and ``metrics.parquet``
only. Traces are a separate download. RCAEval records traces for Online
Boutique and Train Ticket. Sock Shop (``re2ss_*``, ``re3ss_*``) has none, and
this script skips those cases.

The trace bytes are not in ``tools/rcaeval_re23_sha256.txt``. The first
download writes ``traces.parquet.sha256`` next to the file (under
``app/data/``, which is not committed). A later run fails if the bytes change.
This script does not invent an upstream pin.

    python tools/fetch_rcaeval_traces.py --suite RE2
    python tools/fetch_rcaeval_traces.py --suite RE3
    python tools/fetch_rcaeval_traces.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

HF = "https://huggingface.co/datasets/phamquiluan/RCAEval/resolve/main"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "data" / "rcaeval_re23"
MANIFEST = Path(__file__).resolve().parent / "rcaeval_re23_sha256.txt"
# Folder prefix -> suite. Sock Shop is listed so the skip is explicit.
_SKIP_PREFIX = ("re2ss_", "re3ss_")


def cases(suites: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        _sha, _size, rel = line.split()
        folder = rel.split("/", 1)[0]
        if folder in seen:
            continue
        seen.add(folder)
        if folder.lower().startswith(_SKIP_PREFIX):
            continue
        if folder[:3].upper() in suites:
            found.append(folder)
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch_one(folder: str) -> str:
    dest = OUT / folder / "traces.parquet"
    sidecar = dest.with_name(dest.name + ".sha256")
    if dest.is_file() and sidecar.is_file():
        recorded = sidecar.read_text(encoding="ascii").strip().split()
        if recorded and _sha256(dest) == recorded[0].lower():
            return "present"
        raise SystemExit(f"{folder}/traces.parquet does not match its sidecar; refusing to replace it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".parquet.part")
    url = f"{HF}/{folder}/traces.parquet"
    try:
        with urlopen(url, timeout=300) as resp, tmp.open("wb") as handle:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                handle.write(block)
    except HTTPError as exc:
        tmp.unlink(missing_ok=True)
        if exc.code == 404:
            return "missing"
        raise
    digest = _sha256(tmp)
    tmp.replace(dest)
    sidecar.write_text(digest + "\n", encoding="ascii")
    return digest


def check(folders: list[str]) -> int:
    bad = []
    for folder in folders:
        dest = OUT / folder / "traces.parquet"
        sidecar = dest.with_name(dest.name + ".sha256")
        if not dest.is_file() or not sidecar.is_file():
            bad.append(folder)
            continue
        recorded = sidecar.read_text(encoding="ascii").strip().split()
        if not recorded or _sha256(dest) != recorded[0].lower():
            bad.append(folder)
    print("RCAEval trace files OK" if not bad else f"{len(bad)} trace file(s) missing or different")
    return 1 if bad else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", choices=["RE2", "RE3"], action="append")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    folders = cases(tuple(args.suite or ("RE2", "RE3")))
    print(
        f"{len(folders)} Online Boutique and Train Ticket cases. "
        "Sock Shop is skipped: RCAEval has no traces for it."
    )
    if args.check:
        sys.exit(check(folders))
    missing = 0
    got: list[str] = []
    for index, folder in enumerate(folders, 1):
        result = _fetch_one(folder)
        if result == "missing":
            missing += 1
            print(f"  no traces.parquet upstream for {folder}")
        else:
            got.append(folder)
        if index % 25 == 0:
            print(f"  {index}/{len(folders)}")
    print(f"done. {missing} case(s) had no trace file upstream. {len(got)} file(s) hashed.")
    if not got:
        sys.exit(1)
    sys.exit(check(got))


if __name__ == "__main__":
    main()
