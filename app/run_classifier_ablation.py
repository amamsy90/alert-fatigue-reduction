"""Ablation: what the alert classifier adds inside the full queue on GAIA.

Same causal queue as `proposed` in run_queue_eval.py (jumps with the week-1
confirm threshold, leaks, monitoring gaps, and log alerts in at most a quarter
of the K slots). Only the way log alerts are chosen for those slots changes:

  classifier   the cost-sensitive classifier and policy (this is `proposed`)
  severity     log severity + exact duplicates instead of the classifier
  no_alerts    no log alerts at all (metric items only)

Train on week 1, score the next 55 days. The `classifier` row must equal the
`proposed` row in results/queue_eval_gaia.csv; the script checks that.

It also writes results/classifier_ablation_gaia.json with two facts the report
quotes: why the `severity` row attaches no alert at K=20 (what the severity
rule escalates and how far those alerts are from a shown jump), and what the
product's promotion gate decides when it trains the classifier on GAIA.

Usage:
    python run_classifier_ablation.py
    python run_classifier_ablation.py --template-features

`--template-features` compares the current classifier with the optional
template-hash columns on the same first-week split. When the GAIA folders
are absent it scores the synthetic alert sample and says the full GAIA queue
was not run. The published classifier_ablation_gaia.csv rows are unchanged.
"""
from __future__ import annotations

import json
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config
from src.entities import Action, Alert, Decision, Fault
from src.eval_stats import bootstrap_mean_ci
from src.experiment import load_gaia
from src.features import build_feature_frame
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.memory_spikes import Spike, detect_gaps, detect_jumps, detect_leaks
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold
from src.queue import (
    QueueItem,
    alert_items,
    build_queue,
    n_days_between,
    score_queue,
    spike_items,
    tune_confirm_thresholds,
)
from src.scoring import faults_by_service, spike_hits
from src.tracking import GAIA_INPUTS, record_run

VARIANTS = ("classifier", "severity", "no_alerts")


def severity_diagnosis(test_alerts: list[Alert], severity: list[Decision], shown: list) -> dict:
    """Why the severity rule's alerts never ride on a jump (K=20 queue `shown`)."""
    escalated = [a for a, d in zip(test_alerts, severity) if d.action == Action.ESCALATE]
    fault_alerts = [a for a in test_alerts if a.fault_id is not None]
    by_service: dict[str, list[Spike]] = {}
    for item in shown:
        if item.kind != "alert" and item.kind != "gap":
            by_service.setdefault(item.service, []).append(item.spike)
    minutes = [
        min(abs((s.shown_at - a.timestamp).total_seconds()) / 60 for s in by_service[a.source])
        for a in escalated if a.source in by_service
    ]
    shown_alerts = [i for i in shown if i.kind == "alert"]
    return {
        "test_alerts": len(test_alerts),
        "severities": dict(Counter(a.severity for a in test_alerts).most_common()),
        "escalated": len(escalated),
        "escalated_with_fault": sum(a.fault_id is not None for a in escalated),
        "escalated_days": len({a.timestamp.date() for a in escalated}),
        "cut_by_daily_budget": sum(d.reason == "daily budget exhausted" for d in severity),
        "fault_alerts": len(fault_alerts),
        "fault_alert_severities": dict(Counter(a.severity for a in fault_alerts).most_common()),
        "escalated_while_shown_jump_open": sum(
            any(s.open_at(a.timestamp) for s in by_service.get(a.source, [])) for a in escalated
        ),
        "minutes_to_nearest_shown_metric_item_min": round(min(minutes)) if minutes else None,
        "minutes_to_nearest_shown_metric_item_median": round(float(pd.Series(minutes).median())) if minutes else None,
        "alerts_shown": len(shown_alerts),
        "alerts_attached": sum(i.attached for i in shown_alerts),
    }


def promotion_gate_on_gaia(k: int = 20) -> dict:
    """What the product does with GAIA: train, then run the promotion gate (no saved model)."""
    from product.inputs import load_inputs
    from product.pipeline import promotion_margin, score_alerts

    inputs = load_inputs("gaia", budget_k=k)
    result = score_alerts(inputs.alerts, metrics=inputs.metrics, budget_k=k, saved=None, faults=inputs.faults,
                          config=inputs.config, use_saved_detector=False)
    training = result.training or {}
    test = training.get("test") or {}
    return {
        "budget_k": k,
        "model_used": result.model_used,
        "promoted": bool(result.promoted),
        "margin": promotion_margin(result.config or inputs.config),
        "gate": training.get("gate"),
        "held_out_alerts": {key: test.get(key) for key in ("alerts", "fault_alerts", "tp", "fp", "fn")},
        "queue_check": training.get("queue_check"),
    }


def main() -> None:
    config = Config()
    data = load_gaia()
    train_end = data.origin + pd.Timedelta(days=config.split_day)
    test_end = data.last + pd.Timedelta(seconds=1)
    train_alerts = [a for a in data.alerts if a.timestamp < train_end]
    test_alerts = [a for a in data.alerts if train_end <= a.timestamp < test_end]
    train_faults = [f for f in data.faults if f.start < train_end]
    test_faults = [f for f in data.faults if train_end <= f.start < test_end]
    n_days = n_days_between(train_end, test_end, config)

    rows = []
    notes: dict = {}
    template_report: dict | None = None
    for k in config.budget_sweep:
        # Same per-K setup as run_queue_eval.run_window, so `classifier` == `proposed`.
        cfg = replace(config, budget_k=k)
        train_jumps = detect_jumps(data.metrics, cfg, detect_until=train_end)
        test_jumps = detect_jumps(data.metrics, cfg, detect_from=train_end, detect_until=test_end)
        test_gaps = detect_gaps(data.metrics, cfg, detect_from=train_end, detect_until=test_end)
        test_leaks = detect_leaks(data.metrics, cfg, detect_from=train_end, detect_until=test_end)
        thr, _ = tune_confirm_thresholds(train_jumps, train_faults, cfg, train_until=train_end)
        tuned = replace(cfg, confirm_min_rise=thr)

        model = AlertClassifier(cfg).fit(train_alerts, train_jumps)
        alert_thr = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_jumps), cfg)
        proba = model.predict_proba(test_alerts, test_jumps)
        in_jump = [bool(v) for v in build_feature_frame(test_alerts, cfg, test_jumps)["during_memory_spike"]]
        learned = spike_review_decisions(test_alerts, proba, cfg, alert_thr, bayes_threshold(cfg), in_jump)
        severity = baseline_decisions(test_alerts, cfg)

        def metric_items(tuned=tuned, jumps=test_jumps, leaks=test_leaks, gaps=test_gaps):
            return spike_items(jumps, tuned) + spike_items(leaks, tuned) + spike_items(gaps, tuned)

        queues = {
            "classifier": metric_items() + alert_items(test_alerts, learned),
            "severity": metric_items() + alert_items(test_alerts, severity),
            "no_alerts": metric_items(),
        }
        for name in VARIANTS:
            shown = build_queue(queues[name], tuned)
            row = score_queue(name, shown, test_faults, n_days, tuned)
            if name == "severity" and k == 20:
                notes["severity_K20"] = severity_diagnosis(test_alerts, severity, shown)
            rows.append(row)
            print(
                f"K={k:2d}  {name:10s} caught {row['faults_caught']:4d}/{row['faults']} "
                f"({row['detection_rate']:.1%})  items/day {row['items_per_day']:5.2f}  "
                f"false/day {row['false_items_per_day']:4.2f}  precision {row['precision']:.1%}  "
                f"alerts attached {row.get('attached_alerts')}"
            )
        if k == 20:
            # Side report only. The CSV rows above stay the published comparison.
            template_report = evaluate_template_features(
                train_alerts,
                test_alerts,
                test_faults,
                tuned,
                n_days=n_days,
                dataset="gaia",
                train_spikes=train_jumps,
                test_spikes=test_jumps,
                metric_items=metric_items,
            )

    out = Path("results") / "classifier_ablation_gaia.csv"
    out.parent.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"Results written to {out}")

    published = Path("results") / "queue_eval_gaia.csv"
    if published.exists():
        ref = pd.read_csv(published)
        for k in config.budget_sweep:
            want = ref[(ref["queue"] == "proposed") & (ref["budget_k"] == k)]["faults_caught"]
            got = [r["faults_caught"] for r in rows if r["queue"] == "classifier" and r["budget_k"] == k]
            same = len(want) == 1 and got and int(want.iloc[0]) == int(got[0])
            print(f"K={k:2d}  classifier row matches published `proposed`: {'yes' if same else 'NO'}")

    notes["promotion_gate"] = promotion_gate_on_gaia()
    if template_report is not None:
        side = Path("results") / "template_feature_ablation_gaia.json"
        side.write_text(json.dumps(template_report, indent=2) + "\n", encoding="utf-8")
        print(
            f"template features K=20: {template_report['decision']} "
            f"delta {template_report['per_fault_catch_delta']} -> {side}"
        )
    notes_path = Path("results") / "classifier_ablation_gaia.json"
    notes_path.write_text(json.dumps(notes, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(notes, indent=2))

    k20 = {r["queue"]: r for r in rows if r["budget_k"] == 20}
    record_run(
        "classifier_ablation_gaia",
        config=config,
        data=GAIA_INPUTS,
        outputs=[out, notes_path],
        metrics={f"K20.{q}": k20[q]["detection_rate"] for q in VARIANTS},
    )


def _fresh_metric_items(metric_items: Callable[[], list[QueueItem]] | list[QueueItem] | None) -> list[QueueItem]:
    if metric_items is None:
        return []
    if callable(metric_items):
        return list(metric_items())
    return list(metric_items)


def _caught(shown: list[QueueItem], faults: list[Fault]) -> set[str]:
    known = {fault.fault_id for fault in faults}
    by_service = faults_by_service(faults)
    caught: set[str] = set()
    for item in shown:
        if item.kind == "gap":
            continue
        if item.kind == "alert":
            fault_id = item.alert.fault_id
            if fault_id in known:
                caught.add(fault_id)
        else:
            caught.update(
                fault.fault_id for fault in spike_hits(item.spike, by_service) if fault.fault_id in known
            )
    return caught


def _score_variant(
    name: str,
    train_alerts: list[Alert],
    test_alerts: list[Alert],
    test_faults: list[Fault],
    config: Config,
    n_days: int,
    *,
    template_features: bool,
    encoder: object | None,
    train_spikes: list | None,
    test_spikes: list | None,
    metric_items: Callable[[], list[QueueItem]] | list[QueueItem] | None,
) -> tuple[dict, set[str], str | None]:
    model = AlertClassifier(
        config,
        template_features=template_features,
        encoder=encoder if template_features else None,
    )
    model.fit(train_alerts, train_spikes)
    threshold = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_spikes), config)
    proba = model.predict_proba(test_alerts, test_spikes)
    if test_spikes is None:
        in_spike = [False] * len(test_alerts)
    else:
        flags = build_feature_frame(test_alerts, config, test_spikes)["during_memory_spike"]
        in_spike = [bool(value) for value in flags]
    decisions = spike_review_decisions(
        test_alerts, proba, config, threshold, bayes_threshold(config), in_spike
    )
    shown = build_queue(_fresh_metric_items(metric_items) + alert_items(test_alerts, decisions), config)
    row = score_queue(name, shown, test_faults, n_days, config)
    return row, _caught(shown, test_faults), model.template_encoder_name


def evaluate_template_features(
    train_alerts: list[Alert],
    test_alerts: list[Alert],
    test_faults: list[Fault],
    config: Config,
    *,
    n_days: int,
    dataset: str,
    encoder: object | None = None,
    train_spikes: list | None = None,
    test_spikes: list | None = None,
    metric_items: Callable[[], list[QueueItem]] | list[QueueItem] | None = None,
) -> dict:
    """Time-split comparison of the current classifier and the template columns.

    The split is the caller's: train on the first week, score the later days.
    The queue metric is `score_queue` detection rate. The keep/drop rule is the
    95% bootstrap interval (`bootstrap_mean_ci`) of the per-fault catch
    difference (candidate minus baseline). Keep only when that interval lies
    entirely above zero. Otherwise drop. The default queue is unchanged either way.
    """
    baseline, base_caught, _ = _score_variant(
        "baseline",
        train_alerts,
        test_alerts,
        test_faults,
        config,
        n_days,
        template_features=False,
        encoder=None,
        train_spikes=train_spikes,
        test_spikes=test_spikes,
        metric_items=metric_items,
    )
    candidate, cand_caught, encoder_name = _score_variant(
        "template",
        train_alerts,
        test_alerts,
        test_faults,
        config,
        n_days,
        template_features=True,
        encoder=encoder,
        train_spikes=train_spikes,
        test_spikes=test_spikes,
        metric_items=metric_items,
    )
    report: dict = {
        "dataset": dataset,
        "split": "first week train, later days test",
        "split_day": config.split_day,
        "budget_k": config.budget_k,
        "encoder": encoder_name,
        "train_alerts": len(train_alerts),
        "test_alerts": len(test_alerts),
        "test_faults": len(test_faults),
        "baseline_detection_rate": baseline["detection_rate"],
        "candidate_detection_rate": candidate["detection_rate"],
        "baseline_false_items_per_day": baseline["false_items_per_day"],
        "candidate_false_items_per_day": candidate["false_items_per_day"],
        "baseline_faults_caught": baseline["faults_caught"],
        "candidate_faults_caught": candidate["faults_caught"],
    }
    if not test_faults:
        report["per_fault_catch_delta"] = None
        report["decision"] = "drop"
        report["reason"] = "no held-out faults to score"
        return report
    delta = [
        float(fault.fault_id in cand_caught) - float(fault.fault_id in base_caught) for fault in test_faults
    ]
    interval = bootstrap_mean_ci(delta)
    report["per_fault_catch_delta"] = {
        "mean": round(interval.mean, 4),
        "low": round(interval.low, 4),
        "high": round(interval.high, 4),
        "n": interval.n,
    }
    kept = interval.low > 0
    report["decision"] = "keep" if kept else "drop"
    report["reason"] = (
        "the 95% interval for the per-fault catch difference lies above zero"
        if kept
        else "the 95% interval for the per-fault catch difference does not lie above zero"
    )
    return report


def synthetic_template_ablation(days: int = 14, seed: int = 42, budget_k: int = 20) -> dict:
    """Alert-only queue on the built-in sample. Not the GAIA full queue."""
    from src.sample_data import generate

    config = Config(budget_k=budget_k)
    with tempfile.TemporaryDirectory() as folder:
        generate(folder, seed=seed, days=days)
        root = Path(folder)
        alerts = load_alerts(root / "alerts.csv")
        faults = load_faults(root / "faults.csv")
    alerts, faults = align_alerts_to_faults(alerts, faults)
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    _, test_faults = time_split(faults, config.split_day)
    origin = min(alert.timestamp for alert in alerts)
    boundary = origin + pd.Timedelta(days=config.split_day)
    end = max(alert.timestamp for alert in test_alerts) + pd.Timedelta(seconds=1)
    report = evaluate_template_features(
        train_alerts,
        test_alerts,
        test_faults,
        config,
        n_days=n_days_between(boundary, end, config),
        dataset="synthetic",
    )
    report["gaia"] = "skipped: data/gaia_raw and data/gaia_metrics are not in this run"
    return report


def main_template() -> None:
    gaia = Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()
    if gaia:
        print("GAIA folders are present. Scoring the full queue, including template features at K=20.")
        main()
        return
    report = synthetic_template_ablation()
    print(json.dumps(report, indent=2))
    print("GAIA full-queue ablation was not run: data/gaia_raw and data/gaia_metrics are absent.")


if __name__ == "__main__":
    if "--template-features" in sys.argv:
        main_template()
    else:
        main()
