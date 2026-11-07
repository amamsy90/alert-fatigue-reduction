"""Prove that a fresh clone of the repository installs and reproduces, with nothing but git and uv.

    python tools/check_fresh_clone.py                        # clone this repo at HEAD, sync, reproduce
    python tools/check_fresh_clone.py --tests                # also run the full pytest suite
    python tools/check_fresh_clone.py --source https://github.com/amamsy90/alert-fatigue-reduction --ref <hash>

It clones into an empty temporary folder (so no data, venv or cache from this
checkout is reused), installs the locked dependencies with `uv sync --locked`,
and runs `python run_reproduce.py`. Only committed files exist in the clone, so a
file that was never committed makes this fail. Prints one line per step with its
time and ends with PASS or FAIL.
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(label: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> bool:
    start = time.time()
    done = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    ok = done.returncode == 0
    print(f"[{'ok' if ok else 'FAIL'}] {label:34s} {time.time() - start:6.1f} s")
    if not ok:
        print((done.stdout + done.stderr)[-2500:])
    elif label.startswith("reproduce"):
        print("\n".join("       " + line for line in done.stdout.strip().splitlines()))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--tests", action="store_true", help="also run the full pytest suite")
    args = parser.parse_args()

    print(f"{platform.system()} {platform.release()}, Python {platform.python_version()}")
    with tempfile.TemporaryDirectory(prefix="fresh_clone_") as tmp:
        work = Path(tmp) / "repo"
        env = {**os.environ, "UV_LINK_MODE": "copy", "UV_PROJECT_ENVIRONMENT": str(Path(tmp) / "venv")}
        env.pop("VIRTUAL_ENV", None)
        steps = [
            ("git clone", ["git", "clone", "--quiet", args.source, str(work)], Path(tmp)),
            (f"git checkout {args.ref}", ["git", "checkout", "--quiet", args.ref], work),
            ("uv sync --locked --group dev", ["uv", "sync", "--locked", "--group", "dev"], work / "app"),
            ("reproduce (sample)", ["uv", "run", "python", "run_reproduce.py"], work / "app"),
        ]
        if args.tests:
            steps.append(("pytest -q", ["uv", "run", "pytest", "-q"], work / "app"))
        for label, cmd, cwd in steps:
            if not run(label, cmd, cwd, env):
                print("FAIL")
                return 1
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True)
        print(f"commit {head.stdout.strip()}")
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
