#!/usr/bin/env python3
"""Download the pinned GGUF and check its SHA-256.

Weights are not committed. The expected name, revision, and hash live in
``product/llm_pin.json``. A download is kept only when the file hash matches
that pin. ``--check`` only verifies a file that is already on disk.

    python scripts/fetch_llm.py
    python scripts/fetch_llm.py --dest /var/lib/oncall/model.gguf
    python scripts/fetch_llm.py --check /var/lib/oncall/model.gguf

``LLM_GGUF`` selects the same path the app reads when ``--dest`` is omitted.
The script writes ``<file>.sha256`` beside a successful download.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from urllib.request import urlopen

# `python scripts/fetch_llm.py` puts scripts/ on sys.path, not app/.
_APP_ROOT = Path(__file__).resolve().parents[1]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from product.llm import load_pin, sha256_file, weights_path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download or check the pinned local LLM.")
    parser.add_argument("--check", metavar="PATH", help="hash this file and exit 1 on mismatch")
    parser.add_argument("--dest", type=Path, help="where to write the GGUF")
    args = parser.parse_args(argv)
    pin = load_pin()
    expected = str(pin["sha256"])
    if args.check:
        return check(Path(args.check), expected)
    dest = args.dest or weights_path()
    if dest.is_file() and sha256_file(dest) == expected:
        _record(dest, expected)
        print(f"already present {dest}")
        return 0
    download(str(pin["url"]), dest, expected, int(pin["size_bytes"]))
    print(f"ok {expected} {dest}")
    return 0


def check(path: Path, expected: str) -> int:
    if not path.is_file():
        print(f"missing: {path}", file=sys.stderr)
        return 1
    actual = sha256_file(path)
    if actual != expected:
        print(f"hash mismatch\nexpected {expected}\nactual   {actual}", file=sys.stderr)
        return 1
    print(f"ok {actual} {path}")
    return 0


def download(url: str, dest: Path, expected: str, size: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    digest = hashlib.sha256()
    got = 0
    try:
        with urlopen(url, timeout=60) as resp, partial.open("wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
        actual = digest.hexdigest()
        if actual != expected or got != size:
            raise SystemExit(f"hash mismatch\nexpected {expected}\nactual   {actual}\nbytes {got}")
        partial.replace(dest)
        _record(dest, expected)
    finally:
        partial.unlink(missing_ok=True)


def _record(path: Path, expected: str) -> None:
    path.with_name(path.name + ".sha256").write_text(expected + "\n", encoding="ascii")


if __name__ == "__main__":
    sys.exit(main())
