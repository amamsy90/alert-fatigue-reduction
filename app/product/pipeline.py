"""Score a batch into the on-call queue: metric jumps, monitoring gaps, log alerts.

The queue is causal (src/queue.py): items appear when a live system could know
them, first come first served, at most K a day. With no labels it runs on rules
only (metric jumps + gaps + alert severity); with labels or on-call marks the
alert classifier takes over the alert share of the budget.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config, day_of
from src.entities import SURFACED_ACTIONS, Action, Alert, Decision, Fault
from src.features import build_feature_frame
from src.ingestion import time_split
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

from .inputs import (  # noqa: F401 — re-exported for callers of product.pipeline
    JobInputs,
    _default_paths,
    load_alerts_from_dicts,
    load_inputs,
    load_metrics_csv,
)
from .labels import FEEDBACK_PREFIX, apply_feedback, label_source  # noqa: F401
from .model_store import SavedModel, held_out_cost, load_model, register_model
from .monitoring import record_job
from .paging import page_job
from .settings import config_from_env, live_detector
from .store import QueueStore


def _spike_flags(alerts: list[Alert], spikes: list, config: Config) -> list[bool]:
    if not spikes:
        return [False] * len(alerts)
    frame = build_feature_frame(alerts, config, spikes)
    return [bool(v) for v in frame["during_memory_spike"]]


def _apply_model(
    model: AlertClassifier,
    alerts: list[Alert],
    jumps: list,
    flags: list[bool],
    config: Config,
    escalate_threshold: float,
    review_threshold: float,
) -> list[Decision]:
    proba = model.predict_proba(alerts, jumps)
    return spike_review_decisions(alerts, proba, config, escalate_threshold, review_threshold, flags)


def _decisions_holding_out_train(
    alerts: list[Alert],
    jumps: list,
    flags: list[bool],
    config: Config,
    split_at: pd.Timestamp,
    model: AlertClassifier,
    escalate_threshold: float,
    review_threshold: float,
    saved: SavedModel | None,
) -> list[Decision]:
    """Score the held-out window with the model just fit; not the rows it trained on."""
    decisions: list[Decision | None] = [None] * len(alerts)
    early = [i for i, alert in enumerate(alerts) if alert.timestamp < split_at]
    later = [i for i, alert in enumerate(alerts) if alert.timestamp >= split_at]
    if later:
        subset = [alerts[i] for i in later]
        scored = _apply_model(
            model, subset, jumps, [flags[i] for i in later], config, escalate_threshold, review_threshold
        )
        for index, decision in zip(later, scored):
            decisions[index] = decision
    if early:
        subset = [alerts[i] for i in early]
        sub_flags = [flags[i] for i in early]
        if saved is not None and saved.model is not None and saved.label_source != "weak_severity":
            scored = _apply_model(
                saved.model, subset, jumps, sub_flags, config, saved.escalate_threshold, saved.review_threshold
            )
        else:
            scored = baseline_decisions(subset, config)
        for index, decision in zip(early, scored):
            decisions[index] = decision
    if any(decision is None for decision in decisions):
        raise RuntimeError("an alert was left unscored")
    return decisions  # type: ignore[return-value]


def _jump_items(
    spikes: list[Spike],
    alerts: list[Alert],
    decisions: list[Decision],
    shown: set[int] | None = None,
    config: Config | None = None,
    unit: str = "GB",
) -> list[dict]:
    """One row per jump or gap, with the alerts that fell inside it.

    `shown` holds id() of the spikes the causal queue showed; the rest are kept
    (total counts, feedback) but marked shown=False. A jump with no alerts inside
    still matters: it may be a fault the logs missed.
    """
    by_service: dict[str, list[tuple[Alert, Decision]]] = defaultdict(list)
    for alert, decision in zip(alerts, decisions):
        by_service[alert.source].append((alert, decision))
    items = []
    for spike in spikes:
        services = spike.service.split(",")
        inside = [
            decision
            for service in services
            for alert, decision in by_service.get(service, [])
            if spike.start <= alert.timestamp <= spike.end
        ]
        items.append(
            {
                "day": day_of(spike.shown_at, config).date().isoformat(),
                "service": spike.service,
                "start": spike.start.isoformat(),
                "end": spike.end.isoformat(),
                "confirm": spike.shown_at.isoformat(),
                "kind": spike.kind,
                # GB for the built-in memory rule; the detector's own unit otherwise
                "rise_gb": round(spike.rise_at_confirm / 1e9, 3) if unit == "GB" else round(spike.rise_at_confirm, 3),
                "rise": spike.rise_at_confirm,  # metric units (relative mode is not bytes)
                "alerts_inside": len(inside),
                "escalated_inside": sum(d.action == Action.ESCALATE for d in inside),
                "shown": shown is None or id(spike) in shown,
            }
        )
    return items


@dataclass
class ScoredQueue:
    alerts: list[Alert]
    decisions: list[Decision]
    escalate_threshold: float
    review_threshold: float
    spike_flags: list[bool]
    jumps: list[dict]
    # "real" (came with the data), "feedback" (on-call marks), both joined by "+",
    # or "weak_severity" when severity stood in for labels (demo quality only).
    label_source: str
    # "trained" when fit on this data, "saved" when the last saved model scored it.
    model_used: str = "trained"
    model: AlertClassifier | None = None
    train_alerts: int = 0
    training: dict | None = None
    data_fingerprint: str | None = None
    # Jumps and gaps the queue showed (what PagerDuty would page).
    shown_spikes: list | None = None
    config: Config | None = None
    rise_unit: str = "GB"
    detector_label: str = "built-in memory-jump rule"
    # Per-series confirm rise learned on the early window only. Empty if there
    # was no held-out window (the one-day sample) or no labels.
    confirm_by_series: dict | None = None
    # Promotion gate result for a model fit on this batch (None: nothing was fit).
    # A model that fails the gate is returned in `model` only to be kept as a candidate.
    promoted: bool | None = None


def _split(alerts: list[Alert], config: Config) -> tuple[list, list, pd.Timestamp]:
    origin = min(a.timestamp for a in alerts)
    split_at = origin + pd.Timedelta(days=config.split_day)
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    if len(train_alerts) < 5 or len({a.fault_id is not None for a in train_alerts}) < 2:
        # Labels are thin in the first week: train on the first half instead.
        split_at = origin + (max(a.timestamp for a in alerts) - origin) * 0.5
        train_alerts = [a for a in alerts if a.timestamp < split_at]
        test_alerts = [a for a in alerts if a.timestamp >= split_at]
    return train_alerts, test_alerts, split_at


def _class_report(alerts: list[Alert], proba, threshold: float) -> dict:
    """Alert-level scores at the train-chosen escalate threshold."""
    labels = [alert.fault_id is not None for alert in alerts]
    pred = [float(p) >= threshold for p in proba]
    tp = sum(p and y for p, y in zip(pred, labels))
    fp = sum(p and not y for p, y in zip(pred, labels))
    fn = sum((not p) and y for p, y in zip(pred, labels))
    return {
        "alerts": len(alerts),
        "fault_alerts": sum(labels),
        "start": min(a.timestamp for a in alerts).isoformat() if alerts else None,
        "end": max(a.timestamp for a in alerts).isoformat() if alerts else None,
        "precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
        "recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def _rules_only_training(note: str) -> dict:
    return {"model_used": "rules_only", "note": note, "train": None, "test": None, "threshold": None}


def _not_promoted_training(report: dict, fallback: str) -> dict:
    """The fit's held-out report, marked as a candidate that did not score the batch."""
    out = dict(report)
    out["model_used"] = "candidate"
    out["note"] = (
        f"A model was fit on the early window but did not pass the promotion gate ({report.get('gate')}), "
        f"so it is kept as a candidate and {fallback}."
    )
    return out


def _faults_from_labels(alerts: list[Alert]) -> list[Fault]:
    """Faults implied by labelled alerts (Real marks carry no window): one per
    fault id, from its first to its last alert, on the first alert's source."""
    by_id: dict[str, list[Alert]] = defaultdict(list)
    for alert in alerts:
        if alert.fault_id is not None:
            by_id[alert.fault_id].append(alert)
    return [
        Fault(fid, min(a.timestamp for a in rows), max(a.timestamp for a in rows), rows[0].source)
        for fid, rows in by_id.items()
    ]


def _queue_check(
    trained: dict,
    alerts: list[Alert],
    jumps: list,
    leaks: list,
    gaps: list,
    flags: list[bool],
    config: Config,
    faults: list[Fault] | None,
    fallback: SavedModel | None = None,
) -> dict | None:
    """The whole queue on the held-out window three ways: with this model's
    alerts, with the alerts of what scores the batch if the model is refused
    (`fallback`, the active model, or rules only = severity), and with no log
    alerts at all. A classifier can be right about single alerts and still
    lower the queue's catches, because the faults its alerts carry are often also
    caught by metric jumps and its alerts take slots the jumps needed (GAIA,
    FINAL_REPORT.md section B4). None when the held-out window has no fault."""
    split_at = trained["split_at"]
    later = [i for i, a in enumerate(alerts) if a.timestamp >= split_at]
    if not later:
        return None
    later_alerts = [alerts[i] for i in later]
    held_faults = (
        [f for f in faults if f.start >= split_at] if faults else _faults_from_labels(later_alerts)
    )
    if not held_faults:
        return None

    def metric_items() -> list[QueueItem]:  # fresh items per queue: build_queue marks them
        spikes = [s for s in jumps + leaks + gaps if s.shown_at >= split_at]
        return spike_items(spikes, config)

    decisions = _apply_model(
        trained["model"], later_alerts, jumps, [flags[i] for i in later], config,
        trained["escalate_threshold"], trained["review_threshold"],
    )
    end = max(max(a.timestamp for a in later_alerts), max(f.end for f in held_faults)) + pd.Timedelta(seconds=1)
    n_days = n_days_between(split_at, end, config)
    later_flags = [flags[i] for i in later]
    if fallback is not None and fallback.model is not None:
        fallback_name = "the active model"
        fallback_decisions = _apply_model(
            fallback.model, later_alerts, jumps, later_flags, config,
            fallback.escalate_threshold, fallback.review_threshold,
        )
    else:
        fallback_name = "rules only (severity)"
        fallback_decisions = baseline_decisions(later_alerts, config)
    out: dict = {"faults": len(held_faults), "days": n_days, "fallback": fallback_name}
    for name, items in (
        ("with_model", metric_items() + alert_items(later_alerts, decisions)),
        ("fallback", metric_items() + alert_items(later_alerts, fallback_decisions)),
        ("no_log_alerts", metric_items()),
    ):
        row = score_queue(name, build_queue(items, config), held_faults, n_days, config)
        false_items = int(round(row["false_items_per_day"] * n_days))
        missed = len(held_faults) - row["faults_caught"]
        out[f"caught_{name}"] = row["faults_caught"]
        out[f"false_items_{name}"] = false_items
        out[f"cost_{name}"] = config.miss_cost * missed + config.false_alarm_cost * false_items
    return out


def score_alerts(
    alerts: list[Alert],
    *,
    metrics: pd.DataFrame | None = None,
    budget_k: int = 20,
    shadow_mode: bool = True,
    saved: SavedModel | None = None,
    config: Config | None = None,
    faults: list[Fault] | None = None,
    use_saved_detector: bool = True,
    now: pd.Timestamp | None = None,
) -> ScoredQueue:
    """Score EVERY alert in the batch and build the day-by-day causal queue.

    `now` (live jobs): a series silent since before `now - gap_minutes` is shown
    as a gap even if no series sent data since (a dead feed).

    Alert scores come from, in order of preference: a model trained on this
    batch's labels (fit on the early window, reported on the later one), the
    last saved model, or the severity rule (rules-only mode, no labels needed).
    Metric jumps and gaps never need labels. `faults` (e.g. GAIA ground truth)
    lets the jump confirmation threshold be tuned on the early window.
    """
    config = config or config_from_env(budget_k)
    has_metrics = metrics is not None and not metrics.empty
    # Sample mode passes False: that series is bytes sized for the built-in rule.
    # A detector saved from Evaluate-my-data is for another metric.
    detector = live_detector() if use_saved_detector else None
    rise_unit = (
        ("x normal spread" if detector.kind in ("jump", "delta") else "beyond usual range")
        if detector is not None
        else ("GB" if config.memory_jump_mode == "absolute" else "")
    )
    if has_metrics and detector is not None:
        # Settings the team tuned on its own data ("Evaluate my data" -> Use these settings).
        from src.generic import detect as detect_generic

        jumps = detect_generic(metrics, detector, {})
        for jump in jumps:
            jump.kind = "jump"
    else:
        jumps = detect_jumps(metrics, config) if has_metrics else []
    gaps = detect_gaps(metrics, config, now=now) if has_metrics else []
    # Leaks only make sense for byte metrics (the rise is absolute) and the built-in rule.
    leaks = (
        detect_leaks(metrics, config)
        if has_metrics and config.memory_jump_mode == "absolute" and detector is None
        else []
    )

    trained = _train(alerts, metrics if has_metrics else None, config, incumbent=saved)
    confirm_by_series: dict[str, float] = {}
    # Learn the confirm gate on the early window only, and only when a later
    # window exists. The one-day sample has no later window, so it stays on the
    # built-in rule and is not fit on the same points it shows.
    metric_split = trained["split_at"] if trained is not None else None
    if metric_split is None and metrics is not None and has_metrics and faults:
        metric_split = metrics["timestamp"].min() + pd.Timedelta(days=config.split_day)
    has_later = bool(
        metric_split is not None
        and (
            any(j.shown_at >= metric_split for j in jumps)
            or any(f.start >= metric_split for f in faults or [])
        )
    )
    if (
        detector is None
        and config.confirm_min_rise is None
        and faults
        and jumps
        and has_later
    ):
        global_thr, confirm_by_series = tune_confirm_thresholds(
            jumps, faults, config, train_until=metric_split
        )
        config = replace(
            config,
            confirm_min_rise=global_thr,
            confirm_min_rise_by_series=confirm_by_series,
        )
    elif detector is not None and detector.kind == "jump" and detector.levels:
        confirm_by_series = {
            str(sid): float(gate[0]) for sid, gate in detector.levels.items() if gate
        }

    flags = _spike_flags(alerts, jumps, config) if alerts else []
    # The promotion gate runs before any alert is scored: a model fit on this
    # batch scores it only if it beats both "show no log alerts" and the active
    # model on the held-out window. Otherwise it is kept as a candidate and the
    # batch is scored as if it had not been trained (saved model or rules only).
    candidate: AlertClassifier | None = None
    promoted: bool | None = None
    gate = ""
    usable_saved = saved is not None and saved.label_source != "weak_severity" and saved.model is not None
    if trained is not None:
        # The queue check compares the new model with exactly what scores the
        # batch if it is refused: the active model, or rules only (severity).
        trained["training"]["queue_check"] = _queue_check(
            trained, alerts, jumps, leaks, gaps, flags, config, faults,
            fallback=saved if usable_saved else None,
        )
        promoted, gate = promotion_decision(trained["training"], saved is not None, config)
        trained["training"]["promoted"] = promoted
        trained["training"]["gate"] = gate
        if not promoted:
            candidate = trained["model"]
    prefer_saved = usable_saved and (trained is None or not promoted)
    if trained is not None and promoted:
        model = trained["model"]
        escalate_threshold = trained["escalate_threshold"]
        review_threshold = trained["review_threshold"]
        label_source = trained["label_source"]
        model_used, train_count = "trained", trained["train_alerts"]
        training = trained["training"]
        split_at = trained["split_at"]
    elif prefer_saved and saved is not None:
        model = saved.model
        escalate_threshold = saved.escalate_threshold
        review_threshold = saved.review_threshold
        label_source = saved.label_source
        model_used, train_count = "saved", saved.train_alerts
        training = {"model_used": "saved", "note": "This batch had too few labels, so the last saved model scored it.",
                    "threshold": round(float(escalate_threshold), 4), "train": None, "test": None}
        if trained is not None:
            training = _not_promoted_training(trained["training"], "the active model scored this batch")
        split_at = None
    else:
        model = None
        escalate_threshold = review_threshold = 1.0
        label_source, model_used, train_count = "none", "rules_only", 0
        training = _rules_only_training(
            "No labels yet: log alerts are ranked by severity. Metric jumps and gaps do not need labels. "
            "Mark items Real / Noise to train a model."
        )
        if trained is not None:
            training = _not_promoted_training(trained["training"], "rules only (severity) scored this batch")
        split_at = None

    if model is not None and alerts and model_used == "trained" and split_at is not None:
        # The fit just saw the early rows. Score those with the previous model
        # or with severity rules, and use this fit only on the later window.
        decisions = _decisions_holding_out_train(
            alerts, jumps, flags, config, split_at, model, escalate_threshold, review_threshold, saved
        )
    elif model is not None and alerts:
        proba = model.predict_proba(alerts, jumps)
        decisions = spike_review_decisions(alerts, proba, config, escalate_threshold, review_threshold, flags)
        if model_used == "saved" and trained is None:
            training["test"] = _class_report(alerts, proba, escalate_threshold)
    else:
        decisions = baseline_decisions(alerts, config)

    candidates: list[QueueItem] = (
        spike_items(jumps, config) + spike_items(leaks, config) + spike_items(gaps, config)
        + alert_items(alerts, decisions)
    )
    shown = build_queue(candidates, config)
    shown_alerts = {id(item.payload): item for item in shown if item.kind == "alert"}
    final: list[Decision] = []
    for alert, decision in zip(alerts, decisions):
        item = shown_alerts.get(id(alert))
        if decision.action in SURFACED_ACTIONS and item is None:
            final.append(Decision(decision.alert_id, Action.SUPPRESS, decision.confidence, "daily budget used"))
        elif item is not None and item.attached:
            final.append(Decision(decision.alert_id, decision.action, decision.confidence,
                                  f"{decision.reason}; inside a shown memory jump"))
        else:
            final.append(decision)
    shown_spikes = {id(item.payload) for item in shown if item.kind != "alert"}
    _ = shadow_mode  # recorded by caller in the store
    return ScoredQueue(
        alerts=alerts,
        decisions=final,
        escalate_threshold=escalate_threshold,
        review_threshold=review_threshold,
        spike_flags=flags,
        jumps=_jump_items(jumps + leaks + gaps, alerts, final, shown_spikes, config, rise_unit),
        label_source=label_source,
        model_used=model_used,
        model=model if model_used == "trained" else candidate,
        promoted=promoted,
        train_alerts=train_count,
        training=training,
        data_fingerprint=trained["data_fingerprint"] if trained is not None else None,
        shown_spikes=[item.payload for item in shown if item.kind != "alert"],
        config=config,
        rise_unit=rise_unit,
        detector_label=(detector.label() if detector is not None else "built-in memory-jump rule"),
        confirm_by_series=confirm_by_series,
    )


def _train(alerts: list[Alert], metrics: pd.DataFrame | None, config: Config,
           incumbent: SavedModel | None = None) -> dict | None:
    """Fit on the early window of this batch, or None without two label classes.

    No severity stand-in labels: a model that only copies severity adds nothing
    over rules-only mode, and it used to overwrite a better saved model.
    """
    if len(alerts) < 10:
        return None
    train_alerts, test_alerts, split_at = _split(alerts, config)
    if len({a.fault_id is not None for a in train_alerts}) < 2:
        return None
    train_spikes = detect_jumps(metrics, config, detect_until=split_at) if metrics is not None else []
    test_spikes = detect_jumps(metrics, config, detect_from=split_at) if metrics is not None else []
    model = AlertClassifier(config).fit(train_alerts, train_spikes)
    train_proba = model.predict_proba(train_alerts, train_spikes)
    escalate_threshold = tune_threshold(train_alerts, train_proba, config)
    test_proba: Any = model.predict_proba(test_alerts, test_spikes) if test_alerts else []
    # The active model, scored on the same held-out window, is what the new one
    # must beat to be promoted (see product/model_store.register_model).
    incumbent_test = None
    if incumbent is not None and incumbent.model is not None and test_alerts:
        incumbent_proba = incumbent.model.predict_proba(test_alerts, test_spikes)
        incumbent_test = _class_report(test_alerts, incumbent_proba, incumbent.escalate_threshold)
    return {
        "model": model,
        "escalate_threshold": escalate_threshold,
        "review_threshold": bayes_threshold(config),
        "label_source": label_source(train_alerts),
        "train_alerts": len(train_alerts),
        "split_at": split_at,
        "training": {
            "model_used": "trained",
            "note": (
                "Fit on the early window; the threshold is frozen before the later window. "
                "Only the later window is scored with this fit. The early window uses the "
                "previous model, or severity rules when there is none."
            ),
            "threshold": round(float(escalate_threshold), 4),
            "train": _class_report(train_alerts, train_proba, escalate_threshold),
            "test": _class_report(test_alerts, test_proba, escalate_threshold) if test_alerts else None,
            "incumbent_test": incumbent_test,
        },
        "data_fingerprint": _fingerprint(train_alerts),
    }


def _fingerprint(alerts: list[Alert]) -> str:
    """Short hash of the training alerts and their labels (which data made the model)."""
    digest = hashlib.sha256()
    for a in alerts:
        digest.update(f"{a.timestamp.isoformat()}|{a.source}|{a.severity}|{a.message}|{a.fault_id}\n".encode())
    return digest.hexdigest()[:16]


# One job at a time: an Alertmanager retry and a click on "Run" must not score
# the same buffer twice (double rows, and in Live mode double pages).
_JOB_LOCK = threading.Lock()


class JobBusy(RuntimeError):
    """Raised when another scoring job already holds the lock (HTTP 409)."""


# Jobs kept in the database (each ingest job stores 14 days of jumps). Marks
# are copied into the feedback table, so pruning old jobs never loses a label.
DEFAULT_JOB_RETENTION = 200


def run_job(*args, **kwargs) -> dict:
    if not _JOB_LOCK.acquire(blocking=False):
        record_job(kwargs.get("mode", "sample"), "busy", 0.0)
        raise JobBusy("another job is already running; retry shortly")
    start = time.perf_counter()
    outcome = "failed"
    try:
        result = _run_job_locked(*args, **kwargs)
        outcome = "ok"
        return result
    finally:
        _JOB_LOCK.release()
        record_job(kwargs.get("mode", "sample"), outcome, time.perf_counter() - start)


def no_log_alerts_cost(report: dict | None, config: Config) -> float | None:
    """Held-out cost of showing no log alerts at all: every fault-bearing alert is a miss."""
    if not report:
        return None
    return config.miss_cost * report.get("fault_alerts", 0)


def promotion_decision(training: dict | None, has_incumbent: bool, config: Config) -> tuple[bool, str]:
    """Should a newly trained model become the active one (and score this batch)?

    Three tests on this batch's held-out window, all required:

    1. Alerts: its cost (misses x miss_cost + false alarms) is strictly below
       the cost of showing no log alerts at all.
    2. Queue: the whole queue with its alerts costs strictly less (missed
       faults x miss_cost + false items) than both the queue with no log alerts
       and the queue the batch falls back to if the model is refused (the
       active model's alerts, or rules only = severity). So the gate tests the
       model against what would actually run instead.
       A model can pass 1 and fail 2: on GAIA it finds most fault-bearing
       alerts, but those faults are also caught by metric jumps and its alerts
       take slots the jumps needed (FINAL_REPORT.md section B4).
    3. If a model is active, its alert cost is no higher than that model's on
       the same alerts.

    The first model is not promoted just because it is first, and without a
    held-out window or a held-out fault nothing changes.
    """
    training = training or {}
    keep = "the active model stays" if has_incumbent else "rules only stays"
    new = held_out_cost(training.get("test"), config.miss_cost, config.false_alarm_cost)
    floor = no_log_alerts_cost(training.get("test"), config)
    if new is None or floor is None:
        return False, f"no held-out window to test on; {keep}"
    if new >= floor:
        return False, (f"held-out cost {new:g} is not below {floor:g} for showing no log alerts; "
                       f"kept as a candidate, {keep}")
    check = training.get("queue_check")
    if not check:
        return False, f"no held-out fault to test the whole queue on; kept as a candidate, {keep}"
    with_model, without = check["cost_with_model"], check["cost_no_log_alerts"]
    fallback = check.get("cost_fallback", without)
    queue = (
        f"queue with its alerts: {check['caught_with_model']}/{check['faults']} held-out faults, "
        f"{check['false_items_with_model']} false items (cost {with_model:g})"
    )
    if "cost_fallback" in check:
        queue += (
            f"; with {check.get('fallback', 'the fallback')} instead: {check['caught_fallback']}/{check['faults']}, "
            f"{check['false_items_fallback']} false items (cost {fallback:g})"
        )
    queue += (
        f"; without log alerts: {check['caught_no_log_alerts']}/{check['faults']}, "
        f"{check['false_items_no_log_alerts']} false items (cost {without:g})"
    )
    if with_model >= without or with_model >= fallback:
        return False, f"{queue}; kept as a candidate, {keep}"
    if not has_incumbent:
        return True, f"held-out cost {new:g} < {floor:g} for showing no log alerts; {queue}"
    old = held_out_cost(training.get("incumbent_test"), config.miss_cost, config.false_alarm_cost)
    if old is None:
        return False, f"active model not scored on the held-out window; {keep}"
    if new <= old:
        return True, f"held-out cost {new:g} <= active model's {old:g}; {queue}"
    return False, f"held-out cost {new:g} > active model's {old:g}; kept as a candidate, {keep}"


def _save_trained_model(result: ScoredQueue, real_marks: int, loaded: SavedModel | None) -> SavedModel | None:
    """Register the model fitted on this batch as a new version; it becomes the
    active model only if it passes the promotion gate. Returns the active model."""
    if result.model is None:
        return loaded
    saved = SavedModel(
        model=result.model,
        escalate_threshold=result.escalate_threshold,
        review_threshold=result.review_threshold,
        label_source=result.label_source,
        trained_at=pd.Timestamp.now("UTC").isoformat(),
        train_alerts=result.train_alerts,
        feedback_real=real_marks,
    )
    training = result.training or {}
    if result.promoted is not None:
        promote, reason = result.promoted, str(training.get("gate", ""))
    else:
        promote, reason = promotion_decision(result.training, loaded is not None, result.config or Config())
    training = result.training or {}
    entry = register_model(saved, {
        "data_fingerprint": result.data_fingerprint,
        "held_out": training.get("test"),
        "active_model_held_out": training.get("incumbent_test"),
        "gate": reason,
    }, promote)
    if result.training is not None:
        result.training["model_version"] = entry["version"]
        result.training["promoted"] = promote
        result.training["gate"] = reason
    return saved if promote else loaded


def _job_meta(result: ScoredQueue, inputs: JobInputs, saved: SavedModel | None, real_marks: int,
              page: dict, whp: dict, feedback_stats: dict) -> dict:
    decisions = result.decisions
    jumps = result.jumps
    metric_median = None
    if inputs.metrics is not None and not inputs.metrics.empty and "value" in inputs.metrics.columns:
        value = inputs.metrics["value"].median()
        if pd.notna(value):
            metric_median = float(value)

    def shown(kind: str) -> int:
        return sum(1 for j in jumps if j["kind"] == kind and j["shown"])

    return {
        "alert_count": len(result.alerts),
        "escalate": sum(1 for d in decisions if d.action.value == "escalate"),
        "review": sum(1 for d in decisions if d.action.value == "review"),
        "suppress": sum(1 for d in decisions if d.action.value == "suppress"),
        "jumps": sum(1 for j in jumps if j["kind"] == "jump"),
        "jumps_shown": shown("jump"),
        "gap_items": shown("gap"),
        "leak_items": shown("leak"),
        "rise_unit": result.rise_unit,
        "detector": result.detector_label,
        "confirm_by_series": result.confirm_by_series or {},
        "confirm_min_rise": None if result.config is None else result.config.confirm_min_rise,
        "has_metrics": inputs.metrics is not None and not inputs.metrics.empty,
        "metric_median": metric_median,
        "label_source": result.label_source,
        "model_used": result.model_used,
        "model": saved.status(real_marks) if saved else None,
        "training": result.training,
        "pagerduty": page,
        "would_have_paged": whp,
        **feedback_stats,
    }


def _run_job_locked(
    store: QueueStore,
    *,
    mode: str = "sample",
    budget_k: int = 20,
    shadow_mode: bool | None = None,
    alerts_csv: str | Path | None = None,
    metrics_path: str | Path | None = None,
    alert_rows: list[dict] | None = None,
    faults_path: str | Path | None = None,
) -> dict:
    """Load → label from marks → score → save the model → page → store the job."""
    if shadow_mode is None:
        from .ops import get_shadow_mode

        shadow_mode = get_shadow_mode()
    inputs = load_inputs(mode, budget_k=budget_k, alerts_csv=alerts_csv, metrics_path=metrics_path,
                         alert_rows=alert_rows, faults_path=faults_path)
    feedback_stats = apply_feedback(inputs.alerts, store)
    loaded = load_model()
    result = score_alerts(
        inputs.alerts,
        metrics=inputs.metrics,
        budget_k=budget_k,
        shadow_mode=shadow_mode,
        saved=loaded,
        faults=inputs.faults,
        config=inputs.config,
        use_saved_detector=mode != "sample",
        now=pd.Timestamp.now(tz="UTC") if inputs.live_metrics else None,
    )
    real_marks = store.feedback_summary()["real"]
    saved = _save_trained_model(result, real_marks, loaded)
    page, whp = page_job(mode, shadow_mode, result.alerts, result.decisions, result.shown_spikes or [])
    job_id = store.save_job(
        source=inputs.source,
        budget_k=budget_k,
        escalate_threshold=result.escalate_threshold,
        review_threshold=result.review_threshold,
        shadow_mode=shadow_mode,
        alerts=result.alerts,
        decisions=result.decisions,
        spike_flags=result.spike_flags,
        jumps=result.jumps,
        day_offset_hours=result.config.day_offset_hours if result.config else 0.0,
        meta=_job_meta(result, inputs, saved, real_marks, page, whp, feedback_stats),
    )
    try:
        keep = int(os.environ.get("JOB_RETENTION", DEFAULT_JOB_RETENTION))
    except ValueError:
        keep = DEFAULT_JOB_RETENTION
    if keep > 0:
        store.prune_jobs(keep=keep)
    stored = store.job_meta(job_id) or {}
    return {
        "job_id": job_id,
        "source": inputs.source,
        "budget_k": budget_k,
        "shadow_mode": shadow_mode,
        "escalate_threshold": result.escalate_threshold,
        "review_threshold": result.review_threshold,
        "days": store.list_days(job_id),
        "meta": stored.get("meta", {}),
    }
