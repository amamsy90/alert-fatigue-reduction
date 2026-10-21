"""Score a home-lab collection with the repo's queue evaluator.

Per company, and pooled, this calls ``run_window`` and ``score_window``
(the same path as ``app/run_queue_eval.py``). Train is the first
``split_day`` days and test is the later days. A span that short, or an
evaluator that refuses the sample, is written as not scored. No rate is
filled in.

``--learned-ranker`` also trains ``src.ranker.fit_ranker`` on the other
companies' first week and scores the held-out company's later days at
K=20. The keep rule is the one in ``src.model_eval``: the 95% bootstrap
interval on the per-fault catch difference must lie entirely above zero.
The product flags stay off. This script never pages.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .collect import ALERT_COLUMNS, MEMORY_METRIC, METRIC_COLUMNS
from .companies import Company, load_companies
from .names import tenant_source

# Imported from the app when a command runs, so a syntax check of this file
# does not require the app tree. ``_imports`` pulls them in once.
_APP = None


def _imports():
    global _APP
    if _APP is not None:
        return _APP
    from src.config import Config
    from src.entities import Alert, Fault
    from src.eval_stats import bootstrap_mean_ci
    from src.experiment import GaiaData, miss_causes, run_window, score_window
    from src.ingestion import align_alerts_to_faults
    from src.queue import build_queue, n_days_between, score_queue
    from src.ranker import caught_fault_ids, fit_ranker

    _APP = {
        "Config": Config,
        "Alert": Alert,
        "Fault": Fault,
        "bootstrap_mean_ci": bootstrap_mean_ci,
        "GaiaData": GaiaData,
        "miss_causes": miss_causes,
        "run_window": run_window,
        "score_window": score_window,
        "align_alerts_to_faults": align_alerts_to_faults,
        "build_queue": build_queue,
        "n_days_between": n_days_between,
        "score_queue": score_queue,
        "caught_fault_ids": caught_fault_ids,
        "fit_ranker": fit_ranker,
    }
    return _APP


PRODUCT_FLAGS = {
    "LEARNED_RANKER": "off",
    "METRIC_MODEL": "off",
    "LLM_EXPLAIN": "off",
    "SHADOW_MODE": "unchanged",
}

# Same draw count and seed as src.model_eval.EvalSpec, and the same keep test
# (interval.low > 0) as src.model_eval._decision.
BOOTSTRAP_DRAWS = 4000
BOOTSTRAP_SEED = 42
HEADLINE_K = 20


def lab_config(split_day: int = 7):
    """Relative jumps. Boutique containers are tens of megabytes, so the GAIA
    0.5 GB absolute gate would almost never fire. The 50 MB figure is the
    delta(mem[5m]) baseline, recorded with the results."""
    config_cls = _imports()["Config"]
    return config_cls(
        split_day=split_day,
        memory_jump_mode="relative",
        memory_jump_ratio=0.3,
        memory_jump_bytes=5e7,
        leak_rise_bytes=5e7,
    )


def _utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True)


def frames_from_tables(alerts: pd.DataFrame, metrics: pd.DataFrame, faults: pd.DataFrame) -> dict:
    """Build the evaluator frames. ``service`` is ``company/service``."""
    memory = metrics[metrics["metric"].astype(str) == MEMORY_METRIC].copy() if "metric" in metrics.columns else metrics.iloc[0:0].copy()
    if memory.empty and {"timestamp", "value", "service"}.issubset(metrics.columns) and "metric" not in metrics.columns:
        memory = metrics.copy()
    return {"alerts": alerts, "metrics": memory, "faults": faults}


def _prefix_service(frame: pd.DataFrame, companies: list[Company]) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    if "service" not in out.columns and "source" in out.columns:
        return out
    column = "service" if "service" in out.columns else "component"
    if "company" not in out.columns:
        return out

    def one(row: pd.Series) -> str:
        company = str(row["company"])
        raw = str(row[column])
        if raw.startswith(company + "/"):
            return raw
        return tenant_source(company, raw)

    out[column] = out.apply(one, axis=1)
    return out


def _slice(frame: pd.DataFrame, company: str | None) -> pd.DataFrame:
    if company is None or frame.empty or "company" not in frame.columns:
        return frame
    return frame[frame["company"].astype(str) == company].copy()


def _population(alerts: pd.DataFrame, metrics: pd.DataFrame, faults: pd.DataFrame, company: str | None):
    app = _imports()
    alert_rows = _slice(alerts, company)
    metric_rows = _slice(metrics, company)
    fault_rows = _slice(faults, company)
    if "metric" in metric_rows.columns and not metric_rows.empty:
        metric_rows = metric_rows[metric_rows["metric"].astype(str) == MEMORY_METRIC]
    if "status" in fault_rows.columns and not fault_rows.empty:
        fault_rows = fault_rows[fault_rows["status"].astype(str) == "closed"]
    alert_objs = []
    for row in alert_rows.itertuples(index=False):
        alert_objs.append(
            app["Alert"](
                str(row.alert_id),
                pd.Timestamp(row.timestamp),
                str(row.source),
                str(row.alert_type),
                str(row.severity).lower(),
                str(row.message),
            )
        )
    fault_objs = []
    for row in fault_rows.itertuples(index=False):
        fault_objs.append(
            app["Fault"](
                str(row.fault_id),
                pd.Timestamp(row.start),
                pd.Timestamp(row.end),
                str(row.component),
                str(getattr(row, "description", "")),
            )
        )
    if metric_rows.empty:
        metric_frame = pd.DataFrame(columns=["timestamp", "value", "service", "series_id"])
    else:
        metric_frame = pd.DataFrame(
            {
                "timestamp": _utc(metric_rows["timestamp"]),
                "value": pd.to_numeric(metric_rows["value"]),
                "service": metric_rows["service"].astype(str),
            }
        )
        metric_frame["series_id"] = metric_frame["service"]
        metric_frame = metric_frame.dropna(subset=["timestamp", "value"]).sort_values("timestamp")
    for alert in alert_objs:
        alert.timestamp = pd.Timestamp(alert.timestamp)
        if alert.timestamp.tzinfo is None:
            alert.timestamp = alert.timestamp.tz_localize("UTC")
    for fault in fault_objs:
        for field in ("start", "end"):
            stamp = pd.Timestamp(getattr(fault, field))
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            setattr(fault, field, stamp)
    app["align_alerts_to_faults"](alert_objs, fault_objs)
    return metric_frame, alert_objs, fault_objs


def _bounds(metrics: pd.DataFrame, alerts: list, faults: list):
    stamps = []
    if not metrics.empty:
        stamps.append(pd.Timestamp(metrics["timestamp"].min()))
        stamps.append(pd.Timestamp(metrics["timestamp"].max()))
    stamps.extend(alert.timestamp for alert in alerts)
    stamps.extend(fault.start for fault in faults)
    if not stamps:
        return None, None
    origin = min(stamps)
    last = max(stamps)
    return origin, last


def score_population(name: str, metrics: pd.DataFrame, alerts: list, faults: list, config, budgets: list[int]) -> dict:
    """One ``run_window`` per budget. Failure is a reason, not a rate."""
    app = _imports()
    origin, last = _bounds(metrics, alerts, faults)
    if origin is None or last is None:
        return {"name": name, "scored": False, "reason": "no timestamps"}
    if last - origin <= pd.Timedelta(days=config.split_day):
        return {
            "name": name,
            "scored": False,
            "reason": (
                f"the series spans {last - origin}, which is not longer than "
                f"{config.split_day} days, so there is no later-day test window"
            ),
        }
    train_end = origin + pd.Timedelta(days=config.split_day)
    test_end = last + pd.Timedelta(seconds=1)
    test_faults = [fault for fault in faults if train_end <= fault.start < test_end]
    if not test_faults:
        return {
            "name": name,
            "scored": False,
            "reason": "no closed faults start in the test window (after the first week)",
            "train_end": str(train_end),
            "test_end": str(test_end),
        }
    data = app["GaiaData"](alerts=alerts, faults=faults, metrics=metrics, origin=origin, last=last)
    rows: list[dict] = []
    misses: list[dict] = []
    try:
        for budget in budgets:
            cfg = replace(config, budget_k=int(budget))
            run = app["run_window"](data, train_end, test_end, cfg)
            rows.extend(app["score_window"](run, cfg))
            if int(budget) == HEADLINE_K:
                for queue in ("proposed", "jumps_tuned", "delta_5m"):
                    misses.append({"queue": queue, "budget_k": budget, **app["miss_causes"](run, data, cfg, queue)})
    except ValueError as exc:
        return {
            "name": name,
            "scored": False,
            "reason": str(exc),
            "train_end": str(train_end),
            "test_end": str(test_end),
        }
    return {
        "name": name,
        "scored": True,
        "train_end": str(train_end),
        "test_end": str(test_end),
        "test_faults": len(test_faults),
        "rows": rows,
        "misses": misses,
        "jump_mode": config.memory_jump_mode,
        "jump_ratio": config.memory_jump_ratio,
        "delta_rise_bytes": config.memory_jump_bytes,
    }


def _frozen_gate(metrics: pd.DataFrame, faults: list, config, train_end):
    """Confirm-rise gate from this frame only, using jumps and faults before `train_end`.

    Leave-one-company-out passes the other companies here. The held-out series
    is not an argument, so its week cannot move the gate.
    """
    from src.memory_spikes import detect_jumps
    from src.queue import tune_confirm_thresholds

    jumps = detect_jumps(metrics, config, detect_until=train_end) if not metrics.empty else []
    train_faults = [fault for fault in faults if fault.start < train_end]
    gate, _by_series = tune_confirm_thresholds(jumps, train_faults, config, train_until=train_end)
    return None if gate is None else float(gate)


def _alert_model(alerts: list, jumps: list, config):
    """Fit on the training companies' alerts. A one-class sample falls back to severity."""
    from src.classifier import AlertClassifier
    from src.policy import tune_threshold

    if not alerts:
        return None, None
    try:
        model = AlertClassifier(config).fit(alerts, jumps)
        threshold = tune_threshold(alerts, model.predict_proba(alerts, jumps), config)
    except ValueError:
        return None, None
    return model, threshold


def _decisions(alerts: list, jumps: list, config, model, threshold) -> list:
    from src.baseline import baseline_decisions
    from src.features import build_feature_frame
    from src.policy import bayes_threshold, spike_review_decisions

    if model is None or threshold is None or not alerts:
        return baseline_decisions(alerts, config)
    flags = (
        [bool(value) for value in build_feature_frame(alerts, config, jumps)["during_memory_spike"]]
        if jumps
        else [False] * len(alerts)
    )
    return spike_review_decisions(
        alerts,
        model.predict_proba(alerts, jumps),
        config,
        threshold,
        bayes_threshold(config),
        flags,
    )


def _window_items(metrics, alerts, config, start, end, budget: int, confirm_thr, model, threshold):
    """Candidates in [start, end) using a gate and an alert model that were already fit."""
    from src.memory_spikes import detect_gaps, detect_jumps, detect_leaks
    from src.queue import alert_items, spike_items

    if metrics.empty:
        jumps, gaps, leaks = [], [], []
    else:
        jumps = detect_jumps(metrics, config, detect_from=start, detect_until=end)
        gaps = detect_gaps(metrics, config, detect_from=start, detect_until=end)
        leaks = detect_leaks(metrics, config, detect_from=start, detect_until=end)
    window_alerts = [alert for alert in alerts if start <= alert.timestamp < end]
    cfg = replace(config, budget_k=budget, confirm_min_rise=confirm_thr)
    decisions = _decisions(window_alerts, jumps, cfg, model, threshold)
    return (
        spike_items(jumps, cfg)
        + spike_items(leaks, cfg)
        + spike_items(gaps, cfg)
        + alert_items(window_alerts, decisions)
    )


def leave_one_company_out(
    alerts: pd.DataFrame,
    metrics: pd.DataFrame,
    faults: pd.DataFrame,
    companies: list[str],
    config,
) -> dict:
    """Train the slot ranker on every company but one. Score the held-out later days."""
    app = _imports()
    report: dict = {
        "k": HEADLINE_K,
        "rule": "keep only when the 95% bootstrap interval on the per-fault catch difference has lower bound > 0",
        "bootstrap_draws": BOOTSTRAP_DRAWS,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "product_flags": dict(PRODUCT_FLAGS),
        "companies": {},
    }
    if len(companies) < 2:
        report["status"] = "skipped"
        report["reason"] = "leave-one-company-out needs at least two companies"
        return report
    report["status"] = "scored"
    for held in companies:
        others = [company for company in companies if company != held]
        train_metrics, train_alerts, train_faults = _population(alerts, metrics, faults, None)
        # Keep only the other companies. The held-out company is not a training label.
        train_alerts = [alert for alert in train_alerts if alert.source.split("/", 1)[0] in others]
        train_faults = [fault for fault in train_faults if fault.component.split("/", 1)[0] in others]
        if not train_metrics.empty:
            train_metrics = train_metrics[train_metrics["service"].astype(str).str.split("/").str[0].isin(others)].copy()
        test_metrics, test_alerts, test_faults = _population(alerts, metrics, faults, held)
        origin, last = _bounds(test_metrics, test_alerts, test_faults)
        entry: dict = {"train_companies": others, "held_out": held}
        if origin is None or last is None or last - origin <= pd.Timedelta(days=config.split_day):
            entry.update({
                "status": "skipped",
                "decision": "skipped",
                "reason": "held-out series is not longer than the first week",
                "per_fault_catch_delta": None,
                "confirm_min_rise": None,
            })
            report["companies"][held] = entry
            continue
        train_end = origin + pd.Timedelta(days=config.split_day)
        test_end = last + pd.Timedelta(seconds=1)
        # Training labels stop at the held-out company's week boundary.
        train_alerts = [alert for alert in train_alerts if alert.timestamp < train_end]
        train_faults = [fault for fault in train_faults if fault.start < train_end]
        test_faults = [fault for fault in test_faults if train_end <= fault.start < test_end]
        cfg = replace(config, budget_k=HEADLINE_K)
        # Gate and alert model see the other companies only. Held-out week 1 is not a label.
        gate = _frozen_gate(train_metrics, train_faults, cfg, train_end)
        entry["confirm_min_rise"] = gate
        if not test_faults:
            entry.update({"status": "skipped", "decision": "skipped", "reason": "no held-out faults", "per_fault_catch_delta": None})
            report["companies"][held] = entry
            continue
        from src.memory_spikes import detect_jumps

        train_jumps = detect_jumps(train_metrics, cfg, detect_until=train_end) if not train_metrics.empty else []
        model, alert_thr = _alert_model(train_alerts, train_jumps, cfg)
        train_start = train_end
        stamps = [alert.timestamp for alert in train_alerts]
        if not train_metrics.empty:
            stamps.append(pd.Timestamp(train_metrics["timestamp"].min()))
        if stamps:
            train_start = min(stamps)
        train_items = _window_items(
            train_metrics, train_alerts, cfg, train_start, train_end, HEADLINE_K, gate, model, alert_thr
        )
        test_items = _window_items(
            test_metrics, test_alerts, cfg, train_end, test_end, HEADLINE_K, gate, model, alert_thr
        )
        ranker, status = app["fit_ranker"](
            train_items, train_faults, cfg, None, train_end=train_end, backend="pointwise", device="cpu", seed=BOOTSTRAP_SEED
        )
        if ranker is None:
            entry.update({"status": status, "decision": "skipped", "reason": status, "per_fault_catch_delta": None})
            report["companies"][held] = entry
            continue
        baseline = app["build_queue"](test_items, cfg)
        ranked = ranker.select(test_items, None, cfg)
        n_days = app["n_days_between"](train_end, test_end, cfg)
        base_row = app["score_queue"]("baseline", baseline, test_faults, n_days, cfg)
        rank_row = app["score_queue"]("learned_ranker", ranked, test_faults, n_days, cfg)
        base_ids = app["caught_fault_ids"](baseline, test_faults)
        rank_ids = app["caught_fault_ids"](ranked, test_faults)
        delta = [float(fault.fault_id in rank_ids) - float(fault.fault_id in base_ids) for fault in test_faults]
        interval = app["bootstrap_mean_ci"](delta, n_draw=BOOTSTRAP_DRAWS, seed=BOOTSTRAP_SEED)
        kept = interval.low > 0
        entry.update(
            {
                "status": "scored",
                "decision": "keep" if kept else "drop",
                "reason": (
                    "the 95% interval for the per-fault catch difference lies above zero"
                    if kept
                    else "the 95% interval for the per-fault catch difference does not lie above zero"
                ),
                "baseline": {key: base_row[key] for key in ("faults", "faults_caught", "detection_rate", "false_items_per_day", "precision")},
                "learned_ranker": {key: rank_row[key] for key in ("faults", "faults_caught", "detection_rate", "false_items_per_day", "precision")},
                "per_fault_catch_delta": {
                    "mean": round(interval.mean, 4),
                    "low": round(interval.low, 4),
                    "high": round(interval.high, 4),
                    "n": interval.n,
                    "values": delta,
                },
            }
        )
        report["companies"][held] = entry
    return report


def _write_scored(directory: Path, report: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if report.get("scored"):
        pd.DataFrame(report["rows"]).to_csv(directory / "queue_eval.csv", index=False)
        pd.DataFrame(report["misses"]).to_csv(directory / "miss_causes.csv", index=False)
    else:
        (directory / "not_scored.json").write_text(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2) + "\n", encoding="utf-8")


def load_tables(collected: Path, faults_path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    alerts_path = collected / "alerts.csv"
    metrics_path = collected / "metrics.csv"
    alerts = pd.read_csv(alerts_path) if alerts_path.exists() else pd.DataFrame(columns=list(ALERT_COLUMNS))
    metrics = pd.read_csv(metrics_path) if metrics_path.exists() else pd.DataFrame(columns=list(METRIC_COLUMNS))
    if not faults_path.exists():
        raise SystemExit(f"no ground truth at {faults_path}")
    faults = pd.read_csv(faults_path)
    for frame, column in ((alerts, "timestamp"), (metrics, "timestamp")):
        if column in frame.columns and not frame.empty:
            frame[column] = _utc(frame[column])
    for column in ("start", "end"):
        if column in faults.columns and not faults.empty:
            faults[column] = _utc(faults[column])
    companies = sorted(set(faults["company"].astype(str)) | set(metrics.get("company", pd.Series(dtype=str)).astype(str)))
    company_objs = [Company(cid, cid, True, f"company-{cid}", "online-boutique", "steady", "noisy", _dummy_chaos(), "", "") for cid in companies if cid and cid != "nan"]
    alerts = _prefix_service(alerts.rename(columns={"source": "service"}) if "source" in alerts.columns else alerts, company_objs)
    if "service" in alerts.columns and "source" not in alerts.columns:
        alerts = alerts.rename(columns={"service": "source"})
    metrics = _prefix_service(metrics, company_objs)
    if "component" in faults.columns:
        faults = _prefix_service(faults.rename(columns={"component": "service"}), company_objs).rename(columns={"service": "component"})
    return alerts, metrics, faults


def _dummy_chaos():
    from .companies import ChaosSpec

    return ChaosSpec(("pod-kill",), 20, 60, 3, 5)


def run(
    alerts: pd.DataFrame,
    metrics: pd.DataFrame,
    faults: pd.DataFrame,
    *,
    budgets: list[int],
    split_day: int,
    learned_ranker: bool,
    company_ids: list[str],
) -> dict:
    before = {name: os.environ.get(name) for name in ("LEARNED_RANKER", "METRIC_MODEL", "LLM_EXPLAIN")}
    config = lab_config(split_day)
    ranked_ids = [company for company in company_ids if company != "cluster"]
    per_company = {}
    for company in ranked_ids:
        frame, alert_objs, fault_objs = _population(alerts, metrics, faults, company)
        per_company[company] = score_population(company, frame, alert_objs, fault_objs, config, budgets)
    pooled_frame, pooled_alerts, pooled_faults = _population(alerts, metrics, faults, None)
    pooled = score_population("pooled", pooled_frame, pooled_alerts, pooled_faults, config, budgets)
    if learned_ranker:
        loco = leave_one_company_out(alerts, metrics, faults, ranked_ids, config)
    else:
        loco = {
            "status": "off",
            "reason": (
                "Pass --learned-ranker to train the slot ranker on the other companies and "
                "score the held-out company at K=20. LEARNED_RANKER is not set."
            ),
            "product_flags": dict(PRODUCT_FLAGS),
        }
    after = {name: os.environ.get(name) for name in ("LEARNED_RANKER", "METRIC_MODEL", "LLM_EXPLAIN")}
    if before != after:
        raise RuntimeError("evaluation changed product flags")
    return {
        "product_flags": dict(PRODUCT_FLAGS),
        "flags_changed": False,
        "split_day": split_day,
        "budgets": budgets,
        "jump_mode": config.memory_jump_mode,
        "jump_ratio": config.memory_jump_ratio,
        "delta_rise_bytes": config.memory_jump_bytes,
        "per_company": {key: _public(value) for key, value in per_company.items()},
        "pooled": _public(pooled),
        "leave_one_company_out": loco,
        "_reports": {"per_company": per_company, "pooled": pooled},
    }


def _public(report: dict) -> dict:
    """Drop the row payload from the JSON summary; CSVs hold the rows when scored."""
    out = {key: value for key, value in report.items() if key not in {"rows", "misses"}}
    if report.get("scored"):
        out["queues"] = sorted({row["queue"] for row in report["rows"]})
    return out


def write_result(result: dict, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    reports = result.pop("_reports")
    for company, report in reports["per_company"].items():
        _write_scored(dest / "per_company" / company, report)
    _write_scored(dest / "pooled", reports["pooled"])
    (dest / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (dest / "leave_one_company_out.json").write_text(
        json.dumps(result["leave_one_company_out"], indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Score home-lab data with the existing queue evaluator.")
    parser.add_argument("--collected", type=Path, required=True)
    parser.add_argument("--faults", type=Path, required=True)
    parser.add_argument("--companies-file", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--budgets", default="5,10,20,50")
    parser.add_argument("--split-day", type=int, default=7)
    parser.add_argument("--learned-ranker", action="store_true", help="Opt-in leave-one-company-out slot ranker. Does not set LEARNED_RANKER.")
    args = parser.parse_args(argv)
    alerts, metrics, faults = load_tables(args.collected, args.faults)
    ids = [company.id for company in load_companies(args.companies_file) if company.enabled]
    present = sorted(set(faults["company"].astype(str)) & set(ids)) if ids else sorted(set(faults["company"].astype(str)))
    if not present:
        present = sorted(set(faults["company"].astype(str)))
    budgets = [int(part) for part in args.budgets.split(",") if part.strip()]
    result = run(alerts, metrics, faults, budgets=budgets, split_day=args.split_day, learned_ranker=args.learned_ranker, company_ids=present)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = args.out or (Path(__file__).resolve().parents[1] / "homelab" / "results" / stamp)
    write_result(result, dest)
    print(f"wrote {dest}")
    for company, report in result["per_company"].items():
        if report.get("scored"):
            print(f"  {company}: scored, test faults {report.get('test_faults')}")
        else:
            print(f"  {company}: not scored ({report.get('reason')})")


if __name__ == "__main__":
    main()
