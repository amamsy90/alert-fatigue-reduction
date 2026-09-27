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
    _near(float(_row(q, "proposed")["detection_rate"]), 0.702, 0.005, "proposed K=20")
    _near(float(_row(q, "p3_grouped")["detection_rate"]), 0.715, 0.005, "grouped K=20")
    _near(float(_row(q, "p3_carryover")["detection_rate"]), 0.744, 0.005, "carry-over K=20")
    _near(float(_row(q, "delta_5m_carryover")["detection_rate"]), 0.725, 0.005, "delta carry-over K=20")
    _near(float(_row(q, "p3_ranker")["detection_rate"]), 0.702, 0.005, "ranker K=20")
    # Per-series confirm levels (Phase 3 option): same catches as the global gate at
    # K=20 with fewer false items, but it rejects real faults when the budget is loose.
    _near(float(_row(q, "p3_per_series")["detection_rate"]), 0.6999, 0.005, "per-series K=20")
    _near(float(_row(q, "p3_per_series", 50)["detection_rate"]), 0.8406, 0.005, "per-series K=50")
    if float(_row(q, "p3_per_series_jumps")["false_items_per_day"]) >= float(_row(q, "jumps_tuned")["false_items_per_day"]):
        _fail("per-series gates no longer cut false items; rewrite the per-series row")
    _near(float(_row(q, "jumps_hindsight")["detection_rate"]), 0.729, 0.005, "old hindsight K=20")
    _near(float(_row(q, "proposed")["oracle_ceiling"]), 0.849, 0.005, "oracle ceiling K=20")
    if float(_row(q, "proposed")["false_items_per_day"]) >= float(_row(q, "delta_5m")["false_items_per_day"]):
        _fail("proposed must show fewer false items a day than delta_5m")

    m = pd.read_csv(ROOT / "miss_causes_gaia.csv")
    miss = m[(m["queue"] == "proposed") & (m["budget_k"] == 20)].iloc[0]
    _near(float(miss["no_metric_data"]), 166, 2, "misses: no metric data")
    _near(float(miss["budget_full"]), 191, 5, "misses: budget full")
    if int(miss["no_jump"]) + int(miss["below_threshold"]) > 15:
        _fail("detector misses grew; the 'misses are data gaps and budget' claim no longer holds")

    r = pd.read_csv(ROOT / "robustness_gaia.csv").set_index(["shape", "detector"])["detection_rate"]
    _near(float(r[("step_1g", "jump_0.5g")]), 1.0, 0.02, "robustness 1 GB step")
    _near(float(r[("step_0.25g", "jump_0.5g")]), 0.01, 0.03, "robustness 0.25 GB step")
    _near(float(r[("ramp_1g", "jump+leak_3h")]), 0.99, 0.03, "robustness ramp with leak rule")
    _near(float(r[("step_0.5g", "jump_0.5g+threshold")]), 0.0, 0.03, "learned threshold drops 0.5 GB steps")

    s = pd.read_csv(ROOT / "stats_gaia.csv").iloc[0]
    _near(float(s["mean"]), 0.725, 0.01, "walk-forward proposed mean")
    _near(float(s["carry_mean"]), 0.769, 0.01, "walk-forward carry-over mean")
    _near(float(s["delta_carry_mean"]), 0.743, 0.01, "walk-forward delta carry-over mean")
    _near(float(s["false_jumps_near_fault"]), 0.246, 0.02, "false jumps near a fault")
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

    nab = ROOT / "nab_eval.csv"
    if nab.exists():  # only after tools/fetch_nab.py + run_nab.py
        n = pd.read_csv(nab)
        mine = n[(n["queue"] == "your_queue") & (n["budget_k"] == 2)].set_index("dataset")
        _near(float(mine.loc["realTraffic", "detection_rate"]), 0.4545, 0.01, "NAB traffic")
        _near(float(mine.loc["realTweets", "detection_rate"]), 0.3636, 0.01, "NAB tweets")
        if float(mine.loc["realTweets", "detection_rate"]) > float(mine.loc["realTweets", "chance"]) + 0.05:
            _fail("NAB tweets now beats chance; update the 'no better than chance' sentence")

    smd = ROOT / "smd_eval.csv"
    if smd.exists():  # only after tools/fetch_smd.py + run_smd.py
        m = pd.read_csv(smd)
        yq = m[(m["queue"] == "your_queue") & (m["budget_k"] == 20)]
        _near(float(yq["faults_caught"].sum() / yq["faults"].sum()), 0.319, 0.01, "SMD metric movements K=20")
        _near(float(yq["incidents_caught"].sum() / yq["incidents"].sum()), 0.541, 0.01, "SMD incidents K=20")
        yq5 = m[(m["queue"] == "your_queue") & (m["budget_k"] == 5)]
        _near(float(yq5["incidents_caught"].sum() / yq5["incidents"].sum()), 0.306, 0.01, "SMD incidents K=5")

    print("VALIDATION OK")
    for name in ("logs_severity", "static_p99", "delta_5m", "jumps_tuned", "proposed", "p3_carryover"):
        r = _row(q, name)
        print(f"  K=20 {name:14s} {float(r['detection_rate']):.1%}  false/day {float(r['false_items_per_day'])}")
    print(f"  walk-forward proposed {float(s['mean']):.1%} (CI {float(s['ci_low']):.1%}-{float(s['ci_high']):.1%})")


if __name__ == "__main__":
    main()
