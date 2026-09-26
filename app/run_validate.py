"""Check that saved result files still match the published headline numbers.

Run after run_queue_eval.py, run_walkforward.py and run_stats.py. Fails if a
number in FINAL_REPORT.md / VALIDATION.md no longer matches the csv files.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path("results")


def _fail(msg: str) -> None:
    raise SystemExit(f"VALIDATION FAIL: {msg}")


def _near(got: float, expected: float, tol: float, name: str) -> None:
    if abs(got - expected) > tol:
        _fail(f"{name}={got} expected {expected} ± {tol}")


def _row(frame: pd.DataFrame, queue: str, k: int = 20) -> pd.Series:
    return frame[(frame["queue"] == queue) & (frame["budget_k"] == k)].iloc[0]


def main() -> None:
    q = pd.read_csv(ROOT / "queue_eval_gaia.csv")
    _near(float(_row(q, "logs_severity")["detection_rate"]), 0.0, 0.001, "logs baseline K=20")
    _near(float(_row(q, "delta_5m")["detection_rate"]), 0.689, 0.005, "delta_5m K=20")
    _near(float(_row(q, "jumps_tuned")["detection_rate"]), 0.726, 0.005, "jumps_tuned K=20")
    _near(float(_row(q, "proposed")["detection_rate"]), 0.710, 0.005, "proposed K=20")
    _near(float(_row(q, "jumps_hindsight")["detection_rate"]), 0.729, 0.005, "old hindsight K=20")
    _near(float(_row(q, "proposed")["oracle_ceiling"]), 0.849, 0.005, "oracle ceiling K=20")
    if float(_row(q, "proposed")["false_items_per_day"]) >= float(_row(q, "delta_5m")["false_items_per_day"]):
        _fail("proposed must show fewer false items a day than delta_5m")

    s = pd.read_csv(ROOT / "stats_gaia.csv").iloc[0]
    _near(float(s["mean"]), 0.734, 0.01, "walk-forward proposed mean")
    _near(float(s["tuned_mean"]), 0.748, 0.01, "walk-forward jumps_tuned mean")
    _near(float(s["delta_mean"]), 0.707, 0.01, "walk-forward delta_5m mean")
    if int(s["tuned_wins_vs_delta"]) != 8:
        _fail("jumps_tuned must beat delta_5m in all 8 folds")
    _near(float(s["jump_rate_with_data"]), 0.996, 0.005, "jump rule on faults with metric data")
    _near(float(s["chance_overlap"]), 0.026, 0.01, "chance overlap")
    _near(float(s["confirm_minutes_median"]), 0.72, 0.05, "confirm minutes")
    if float(s["weakest_with_data"]) > 0.7:
        _fail("weakest fold is no longer the metric outage week; rewrite the fold-7 paragraph")
    if str(s["injection_tags"]) != "memory_anomalies:1367":
        _fail("every injected fault must still be a memory anomaly")

    print("VALIDATION OK")
    for name in ("logs_severity", "static_p99", "delta_5m", "jumps_tuned", "proposed"):
        r = _row(q, name)
        print(f"  K=20 {name:14s} {float(r['detection_rate']):.1%}  false/day {float(r['false_items_per_day'])}")
    print(f"  walk-forward proposed {float(s['mean']):.1%} (CI {float(s['ci_low']):.1%}-{float(s['ci_high']):.1%})")


if __name__ == "__main__":
    main()
