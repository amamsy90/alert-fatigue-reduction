"""Check that the result files still match the numbers printed in FINAL_REPORT.md.

Run after run_queue_eval.py, run_walkforward.py, run_stats.py, run_robustness.py
(and run_nab.py / run_smd.py / run_rcaeval.py when their data is present).

The report's "Checked numbers" table is the contract: every row names a key and
the value the report prints. Each key is recomputed from app/results/ and must
round to the printed value (a printed 69.6% passes for 0.6955-0.6965). A key the
report prints but this script cannot compute, or a computable key the report
leaves out, is a failure too, so the text and the checks cannot drift apart.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path("results")
REPORT = Path(__file__).resolve().parent.parent / "FINAL_REPORT.md"
TABLE_HEADING = "## Checked numbers"


RCA_APPS = {"Online Boutique": "ob", "Sock Shop": "ss", "Train Ticket": "tt"}


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

    ablation = ROOT / "classifier_ablation_gaia.csv"
    if ablation.exists():  # run_classifier_ablation.py
        a = pd.read_csv(ablation)
        for name in ("classifier", "severity", "no_alerts"):
            out[f"ablation.K20.{name}"] = float(_row(a, name)["detection_rate"])
            out[f"ablation.false.K20.{name}"] = float(_row(a, name)["false_items_per_day"])
        out["ablation.attached.K20.classifier"] = float(_row(a, "classifier")["attached_alerts"])
        # What the tool runs on GAIA: the gate refuses the classifier, so log alerts come from severity.
        out["ablation.K20.shipped"] = float(_row(a, "severity")["detection_rate"])
        out["ablation.false.K20.shipped"] = float(_row(a, "severity")["false_items_per_day"])

    notes = ROOT / "classifier_ablation_gaia.json"
    if notes.exists():  # run_classifier_ablation.py: why severity attaches nothing, and the product's gate
        facts = json.loads(notes.read_text(encoding="utf-8"))
        sev = facts["severity_K20"]
        out["severity.K20.fault_alerts"] = float(sev["fault_alerts"])
        out["severity.K20.fault_alerts_warning"] = float(sev["fault_alert_severities"].get("warning", 0))
        out["severity.K20.fault_alerts_info"] = float(sev["fault_alert_severities"].get("info", 0))
        for key in ("escalated", "escalated_with_fault", "escalated_days", "cut_by_daily_budget",
                    "escalated_while_shown_jump_open", "alerts_shown"):
            out[f"severity.K20.{key}"] = float(sev[key])
        out["severity.K20.minutes_to_shown_metric_min"] = float(sev["minutes_to_nearest_shown_metric_item_min"])
        out["severity.K20.minutes_to_shown_metric_median"] = float(sev["minutes_to_nearest_shown_metric_item_median"])
        gate = facts["promotion_gate"]
        check = gate["queue_check"]
        out["gate.held_out.fault_alerts_found"] = float(gate["held_out_alerts"]["tp"])
        out["gate.queue.faults"] = float(check["faults"])
        out["gate.queue.caught_with_model"] = float(check["caught_with_model"])
        out["gate.queue.caught_no_log_alerts"] = float(check["caught_no_log_alerts"])
        out["gate.queue.false_with_model"] = float(check["false_items_with_model"])
        out["gate.queue.false_no_log_alerts"] = float(check["false_items_no_log_alerts"])
        out["gate.queue.caught_fallback"] = float(check["caught_fallback"])
        out["gate.queue.false_fallback"] = float(check["false_items_fallback"])
        out["gate.margin"] = float(gate["margin"])

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

    rca = ROOT / "rcaeval_mem.csv"
    if rca.exists():  # only after tools/fetch_rcaeval_mem.py + run_rcaeval.py
        frame = pd.read_csv(rca)
        settings = pd.read_csv(ROOT / "rcaeval_mem_settings.csv")
        for dataset in frame["dataset"].unique():
            jump = frame[(frame["dataset"] == dataset) & (frame["queue"] == "jump_rule") & (frame["budget_k"] == 5)].iloc[0]
            yours = frame[(frame["dataset"] == dataset) & (frame["queue"] == "your_queue") & (frame["budget_k"] == 5)].iloc[0]
            fit = settings[(settings["dataset"] == dataset) & (settings["kind"] == "jump")].iloc[0]
            out[f"rcaeval.{dataset}.rise_gb"] = float(jump["median_rise_gb"])
            out[f"rcaeval.{dataset}.jump_spikes"] = float(fit["test_spikes"])
            out[f"rcaeval.{dataset}.jump_open"] = float(jump["detection_no_budget"])
            out[f"rcaeval.{dataset}.jump_chance"] = float(jump["chance"])
            out[f"rcaeval.{dataset}.jump_train_false"] = float(fit["train_false"])
            out[f"rcaeval.{dataset}.K5"] = float(yours["detection_rate"])

    resources = ROOT / "resources_gaia.json"
    if resources.exists():  # perf/resources.py (needs GAIA)
        used = json.loads(resources.read_text(encoding="utf-8"))
        for field in ("wall_seconds", "peak_rss_mb", "peak_rss_mb_before_job", "db_mb", "db_kb_per_day"):
            out[f"resources.{field}"] = float(used[field])

    logs = ROOT / "rcaeval_logs.csv"
    if logs.exists():  # only after tools/fetch_rcaeval_re23.py + run_rcaeval_logs.py
        frame = pd.read_csv(logs)
        facts = json.loads((ROOT / "rcaeval_logs.json").read_text(encoding="utf-8"))
        jumps = sum(app["jumps"] for app in facts["apps"])
        out["rcalogs.early_jump_share"] = sum(app["early_jumps"] for app in facts["apps"]) / jumps
        for app in facts["apps"]:
            suite, short = app["suite"], RCA_APPS[app["app"]]
            rows = frame[(frame["suite"] == suite) & (frame["app"] == app["app"]) & (frame["budget_k"] == 10)]
            key = f"rcalogs.{suite}.{short}"
            for name, label in (("no_alerts", "none"), ("severity", "severity"), ("classifier", "classifier")):
                row = rows[rows["queue"] == name].iloc[0]
                out[f"{key}.{label}"] = float(row["detection_rate"])
                out[f"{key}.false.{label}"] = float(row["false_items_per_day"])
            best = rows[rows["queue"] == "classifier"].iloc[0]
            out[f"{key}.classifier.ci_low"] = float(best["ci_low"])
            out[f"{key}.classifier.ci_high"] = float(best["ci_high"])
            diff = app["by_k"]["10"]["classifier_minus_no_alerts"]["detection"]
            out[f"{key}.diff.ci_low"] = float(diff[1])
            out[f"{key}.diff.ci_high"] = float(diff[2])
            out[f"{key}.placebo"] = float(app["by_k"]["10"]["placebo_caught"])
            if key == "rcalogs.RE2.tt":  # the "+13.3 over none (+2.2 over severity)" sentences (§5, §12, §13)
                rate = {name: float(rows[rows["queue"] == name].iloc[0]["detection_rate"])
                        for name in ("classifier", "severity", "no_alerts")}
                out[f"{key}.gain_vs_none"] = rate["classifier"] - rate["no_alerts"]
                out[f"{key}.gain_vs_severity"] = rate["classifier"] - rate["severity"]
            out[f"{key}.test_cases"] = float(app["test_cases"])
            gate = app["promotion_gate"]
            out["rcalogs.gate.margin"] = float(gate["margin"])
            if (gate.get("queue_saving") or 0) > 0:  # cheaper than both alternatives: the margin decides
                check = gate["queue_check"]
                for field in ("faults", "caught_with_model", "false_items_with_model", "caught_fallback",
                              "false_items_fallback", "caught_no_log_alerts", "false_items_no_log_alerts"):
                    out[f"{key}.gate.{field}"] = float(check[field])
                out[f"{key}.gate.saving"] = float(gate["queue_saving"])
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
    ablation = ROOT / "classifier_ablation_gaia.csv"
    if ablation.exists():
        a = pd.read_csv(ablation)
        for k in sorted(a["budget_k"].unique()):
            if int(_row(a, "classifier", k)["faults_caught"]) != int(_row(q, "proposed", k)["faults_caught"]):
                _fail(f"ablation 'classifier' row at K={k} no longer equals the published full queue")
        if float(_row(a, "classifier")["detection_rate"]) > float(_row(a, "no_alerts")["detection_rate"]):
            _fail("the classifier now adds catches on GAIA; rewrite the 'adds no catches' sentences (§5, §12, §B4)")
    notes = ROOT / "classifier_ablation_gaia.json"
    if notes.exists():
        facts = json.loads(notes.read_text(encoding="utf-8"))
        gate = facts["promotion_gate"]
        if gate["queue_check"].get("fallback") != "rules only (severity)":
            _fail("the GAIA gate no longer falls back to severity rules; the 'shipped configuration' row is wrong")
        if gate["promoted"] or gate["model_used"] != "rules_only":
            _fail("the promotion gate now promotes the classifier on GAIA; rewrite §5, §9, §12 and §B4")
        if facts["severity_K20"]["alerts_attached"] != 0:
            _fail("the severity row now attaches alerts; rewrite the 'why the severity row attaches no alerts' paragraph")
    nab = ROOT / "nab_eval.csv"
    if nab.exists():
        n = pd.read_csv(nab)
        tweets = n[(n["queue"] == "your_queue") & (n["budget_k"] == 2) & (n["dataset"] == "realTweets")].iloc[0]
        if float(tweets["detection_rate"]) > float(tweets["chance"]) + 0.05:
            _fail("NAB tweets now beats chance; update the 'no better than chance' sentence")
    rca = ROOT / "rcaeval_mem.csv"
    if rca.exists():
        frame = pd.read_csv(rca)
        yours = frame[frame["queue"] == "your_queue"]
        if (yours["detection_rate"] > 0).any():
            _fail("RCAEval queue now catches a test fault; update the §B5 paragraph")
        jump = frame[(frame["queue"] == "jump_rule") & (frame["budget_k"] == 5)]
        if (jump["detection_no_budget"] <= jump["chance"]).any():
            _fail("RCAEval jump rule is no longer above its chance line; update §B5")
    logs = ROOT / "rcaeval_logs.json"
    if logs.exists():
        facts = json.loads(logs.read_text(encoding="utf-8"))
        promoted = sorted(f"{a['suite']} {a['app']}" for a in facts["apps"] if a["promotion_gate"]["promoted"])
        if promoted != ["RE2 Train Ticket"]:
            _fail(f"the gate now promotes the classifier on {promoted}; rewrite the RCAEval RE2/RE3 gate sentences (§B5)")
        by_margin = sorted(f"{a['suite']} {a['app']}" for a in facts["apps"]
                           if not a["promotion_gate"]["promoted"] and (a["promotion_gate"].get("queue_saving") or 0) > 0)
        if by_margin != ["RE2 Online Boutique"]:
            _fail(f"the margin now refuses {by_margin}; rewrite the margin sentences (§9, §B5)")
        tt = next(a["promotion_gate"] for a in facts["apps"] if a["suite"] == "RE2" and a["app"] == "Train Ticket")
        if tt["queue_saving"] != tt["margin"]:
            _fail("RE2 Train Ticket no longer passes exactly at the margin; rewrite 'exactly one missed fault' (§B5)")
        if any(a["by_k"]["10"]["classifier_minus_no_alerts"]["cases_lost"] for a in facts["apps"]):
            _fail("the classifier now loses a case to 'no log alerts' at K = 10 on RCAEval; rewrite §B5")


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
