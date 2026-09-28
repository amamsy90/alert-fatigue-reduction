"""Check that the result files still match the numbers printed in FINAL_REPORT.md.

Run after run_queue_eval.py, run_walkforward.py, run_stats.py, run_robustness.py
(and run_nab.py / run_smd.py when their data is present).

The report's "Checked numbers" table is the contract: every row names a key and
the value the report prints. Each key is recomputed from app/results/ and must
round to the printed value (a printed 69.6% passes for 0.6955-0.6965). A key the
report prints but this script cannot compute, or a computable key the report
leaves out, is a failure too, so the text and the checks cannot drift apart.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

ROOT = Path("results")
REPORT = Path(__file__).resolve().parent.parent / "FINAL_REPORT.md"
TABLE_HEADING = "## Checked numbers"


def _fail(msg: str) -> None:
    raise SystemExit(f"VALIDATION FAIL: {msg}")


def _row(frame: pd.DataFrame, queue: str, k: int = 20) -> pd.Series:
    return frame[(frame["queue"] == queue) & (frame["budget_k"] == k)].iloc[0]


def report_numbers(path: Path = REPORT) -> dict[str, str]:
    """key -> printed value, from the report's Checked numbers table."""
    text = path.read_text(encoding="utf-8")
    if TABLE_HEADING not in text:
        _fail(f"{path.name} has no '{TABLE_HEADING}' section")
    section = text.split(TABLE_HEADING, 1)[1].split("\n## ", 1)[0]
    out: dict[str, str] = {}
    for line in section.splitlines():
        match = re.match(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|", line)
        if match:
            out[match.group(1)] = match.group(2)
    if not out:
        _fail(f"the '{TABLE_HEADING}' table in {path.name} is empty")
    return out


def matches(computed: float, printed: str) -> bool:
    """True when `computed` rounds to `printed` ("69.6%" for a rate, "198" or "0.72" for a number)."""
    printed = printed.strip()
    percent = printed.endswith("%")
    number = printed.rstrip("%").strip()
    decimals = len(number.split(".", 1)[1]) if "." in number else 0
    value = computed * 100 if percent else computed
    return abs(value - float(number)) <= 0.5 * 10 ** -decimals + 1e-9


def computed_numbers() -> dict[str, float]:
    """Every checkable key, recomputed from the csv files."""
    out: dict[str, float] = {}
    q = pd.read_csv(ROOT / "queue_eval_gaia.csv")
    for name in ("logs_severity", "static_p99", "delta_5m", "jumps_hindsight", "jumps_online", "jumps_tuned",
                 "proposed", "p3_ranker", "p3_grouped", "p3_carryover", "delta_5m_carryover", "p3_per_series"):
        out[f"queue.K20.{name}"] = float(_row(q, name)["detection_rate"])
    for name in ("delta_5m", "jumps_tuned", "proposed", "p3_per_series_jumps"):
        out[f"false.K20.{name}"] = float(_row(q, name)["false_items_per_day"])
    out["queue.K50.proposed"] = float(_row(q, "proposed", 50)["detection_rate"])
    out["queue.K50.p3_per_series"] = float(_row(q, "p3_per_series", 50)["detection_rate"])
    out["ceiling.K20"] = float(_row(q, "proposed")["oracle_ceiling"])

    m = pd.read_csv(ROOT / "miss_causes_gaia.csv")
    miss = m[(m["queue"] == "proposed") & (m["budget_k"] == 20)].iloc[0]
    for col in ("no_metric_data", "budget_full", "no_jump", "below_threshold"):
        out[f"misses.K20.{col}"] = float(miss[col])

    r = pd.read_csv(ROOT / "robustness_gaia.csv").set_index(["shape", "detector"])["detection_rate"]
    for shape, detector in (("step_1g", "jump_0.5g"), ("step_0.25g", "jump_0.5g"),
                            ("ramp_1g", "jump+leak_3h"), ("step_0.5g", "jump_0.5g+threshold")):
        out[f"robustness.{shape}.{detector}"] = float(r[(shape, detector)])

    s = pd.read_csv(ROOT / "stats_gaia.csv").iloc[0]
    for key, col in (("walkforward.proposed.mean", "mean"), ("walkforward.proposed.ci_low", "ci_low"),
                     ("walkforward.proposed.ci_high", "ci_high"), ("walkforward.proposed.wins", "wins_vs_delta"),
                     ("walkforward.jumps_tuned.mean", "tuned_mean"), ("walkforward.jumps_tuned.wins", "tuned_wins_vs_delta"),
                     ("walkforward.delta_5m.mean", "delta_mean"), ("walkforward.carryover.mean", "carry_mean"),
                     ("walkforward.delta_carryover.mean", "delta_carry_mean"),
                     ("stats.jump_rate_with_data", "jump_rate_with_data"), ("stats.chance_overlap", "chance_overlap"),
                     ("stats.confirm_minutes_median", "confirm_minutes_median"),
                     ("stats.false_jumps_near_fault", "false_jumps_near_fault")):
        out[key] = float(s[col])

    nab = ROOT / "nab_eval.csv"
    if nab.exists():  # only after tools/fetch_nab.py + run_nab.py
        n = pd.read_csv(nab)
        mine = n[(n["queue"] == "your_queue") & (n["budget_k"] == 2)].set_index("dataset")
        for dataset in mine.index:
            out[f"nab.{dataset}"] = float(mine.loc[dataset, "detection_rate"])

    smd = ROOT / "smd_eval.csv"
    if smd.exists():  # only after tools/fetch_smd.py + run_smd.py
        frame = pd.read_csv(smd)
        for k in (5, 10, 20):
            yq = frame[(frame["queue"] == "your_queue") & (frame["budget_k"] == k)]
            out[f"smd.K{k}.incidents"] = float(yq["incidents_caught"].sum() / yq["incidents"].sum())
            out[f"smd.K{k}.movements"] = float(yq["faults_caught"].sum() / yq["faults"].sum())
    return out


def claims_still_hold() -> None:
    """Sentences in the report that are not a single number."""
    q = pd.read_csv(ROOT / "queue_eval_gaia.csv")
    if float(_row(q, "p3_per_series_jumps")["false_items_per_day"]) >= float(_row(q, "jumps_tuned")["false_items_per_day"]):
        _fail("per-series gates no longer cut false items; rewrite the per-series row")
    if float(_row(q, "proposed")["false_items_per_day"]) >= float(_row(q, "delta_5m")["false_items_per_day"]):
        _fail("proposed must show fewer false items a day than delta_5m")
    m = pd.read_csv(ROOT / "miss_causes_gaia.csv")
    miss = m[(m["queue"] == "proposed") & (m["budget_k"] == 20)].iloc[0]
    if int(miss["no_jump"]) + int(miss["below_threshold"]) > 15:
        _fail("detector misses grew; the 'misses are data gaps and budget' claim no longer holds")
    s = pd.read_csv(ROOT / "stats_gaia.csv").iloc[0]
    if int(s["tuned_wins_vs_delta"]) != 8:
        _fail("jumps_tuned must beat delta_5m in all 8 folds")
    if float(s["weakest_with_data"]) > 0.7:
        _fail("weakest fold is no longer the metric outage week; rewrite the fold-7 paragraph")
    if str(s["injection_tags"]) != "memory_anomalies:1367":
        _fail("every injected fault must still be a memory anomaly")
    nab = ROOT / "nab_eval.csv"
    if nab.exists():
        n = pd.read_csv(nab)
        tweets = n[(n["queue"] == "your_queue") & (n["budget_k"] == 2) & (n["dataset"] == "realTweets")].iloc[0]
        if float(tweets["detection_rate"]) > float(tweets["chance"]) + 0.05:
            _fail("NAB tweets now beats chance; update the 'no better than chance' sentence")


def main() -> None:
    printed = report_numbers()
    computed = computed_numbers()
    unknown = sorted(set(printed) - set(computed))
    if unknown:
        _fail(f"the report checks keys this script cannot compute (missing results?): {', '.join(unknown)}")
    unchecked = sorted(set(computed) - set(printed))
    if unchecked:
        _fail(f"add these keys to the report's Checked numbers table: {', '.join(unchecked)}")
    wrong = [f"{key}: report {printed[key]}, results {computed[key]:.4f}"
             for key in sorted(printed) if not matches(computed[key], printed[key])]
    if wrong:
        _fail("report and results disagree:\n  " + "\n  ".join(wrong))
    claims_still_hold()
    print(f"VALIDATION OK ({len(printed)} numbers in {REPORT.name} match app/results/)")


if __name__ == "__main__":
    main()
