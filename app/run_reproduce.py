"""One command to check that this checkout reproduces the GAIA results.

    python run_reproduce.py            # sample: real GAIA, first 10 days, about 20 seconds
    python run_reproduce.py --full     # all 62 days (needs the GAIA download), then run_validate.py
    python run_reproduce.py --update   # re-record the sample's expected counts (after a deliberate change)

Sample mode runs the same code as the headline table (src/experiment.py:run_window),
trains on the first 7 days, scores days 8-10 at K = 10 and 20, and compares the
caught-fault counts with examples/gaia_sample/expected.json. The counts are exact
integers from a deterministic pipeline, so any difference fails. It also checks
the one claim that must hold at this scale: at K = 20 the learned-threshold jump
detector catches more faults than the 5-minute rate rule.

The sample is a smoke-scale check (59 faults), not the 1,223-fault result. Full
mode reproduces the published tables and then fails if FINAL_REPORT.md disagrees.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.config import Config
from src.experiment import load_gaia, run_window, score_window

SAMPLE = Path("examples/gaia_sample")
EXPECTED = SAMPLE / "expected.json"
BUDGETS = (10, 20)
QUEUES = ("static_p99", "delta_5m", "jumps_tuned", "proposed")


def sample_counts() -> dict[str, dict[str, list[int]]]:
    config = Config()
    data = load_gaia(str(SAMPLE / "run"), str(SAMPLE / "metric"))
    split_at = data.origin + pd.Timedelta(days=config.split_day)
    end = data.last + pd.Timedelta(seconds=1)
    out: dict[str, dict[str, list[int]]] = {}
    for k in BUDGETS:
        cfg = replace(config, budget_k=k)
        rows = {r["queue"]: r for r in score_window(run_window(data, split_at, end, cfg), cfg)}
        out[f"K{k}"] = {q: [int(rows[q]["faults_caught"]), int(rows[q]["faults"])] for q in QUEUES}
    return out


def check_sample(update: bool) -> int:
    got = sample_counts()
    for key, queues in got.items():
        for queue, (caught, total) in queues.items():
            print(f"{key:4s} {queue:12s} caught {caught:3d}/{total}  ({caught / total:.1%})")
    if update:
        EXPECTED.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
        print(f"Recorded {EXPECTED}")
        return 0
    failures = []
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    if got != expected:
        failures.append(f"counts differ from {EXPECTED}:\n  got      {got}\n  expected {expected}")
    if got["K20"]["jumps_tuned"][0] <= got["K20"]["delta_5m"][0]:
        failures.append("claim failed: at K=20 the jump detector no longer beats the rate rule")
    for line in failures:
        print("FAIL:", line)
    if not failures:
        print("REPRODUCED: sample counts match and the detector beats the rate rule at K=20")
    return 1 if failures else 0


def check_full() -> int:
    if not Path("data/gaia_metrics").exists() or not Path("data/gaia_raw/run/run").exists():
        print("Full GAIA data not found: run  python ../tools/fetch_gaia.py  first", file=sys.stderr)
        return 2
    for script in ("run_queue_eval.py", "run_baseline_compare.py", "run_validate.py"):
        print(f"\n== {script}")
        code = subprocess.call([sys.executable, script])
        if code:
            return code
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="reproduce the published tables (needs GAIA)")
    parser.add_argument("--update", action="store_true", help="re-record the sample's expected counts")
    args = parser.parse_args()
    return check_full() if args.full else check_sample(args.update)


if __name__ == "__main__":
    sys.exit(main())
