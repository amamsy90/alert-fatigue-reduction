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
from src.drift import drift_report
from src.entities import SURFACED_ACTIONS, Action, Alert, Decision, Fault
from src.memory_spikes import Spike, detect_gaps, detect_jumps, detect_leaks
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold
from src.queue import (
    QueueItem,
    alert_items,
    spike_items,
    tune_confirm_thresholds,
)
from src.template_features import template_prior_mean

from .inputs import (  # noqa: F401 — re-exported for callers of product.pipeline
    JobInputs,
    _default_paths,
    load_alerts_from_dicts,
    load_inputs,
    load_metrics_csv,
)
from .labels import FEEDBACK_PREFIX, apply_feedback, label_source  # noqa: F401
from .model_store import SavedModel, load_model, register_model
from .monitoring import record_job
from .paging import page_job
from .promotion import no_log_alerts_cost, promotion_decision, promotion_margin  # noqa: F401 — re-exported
from .queue_check import apply_model, queue_check, spike_flags
from .settings import config_from_env, live_detector
from .store import QueueStore
from .training_report import (
    class_report,
    not_promoted_training,
    rules_only_training,
    split_for_training,
)


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
        scored = apply_model(
            model, subset, jumps, [flags[i] for i in later], config, escalate_threshold, review_threshold
        )
        for index, decision in zip(later, scored):
            decisions[index] = decision
    if early:
        subset = [alerts[i] for i in early]
        sub_flags = [flags[i] for i in early]
        if saved is not None and saved.model is not None and saved.label_source != "weak_severity":
            scored = apply_model(
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
    # Optional metric model / learned ranker. Both are "off" unless the flags are set.
    optional_models: dict | None = None
    # Input drift between the early window the model learns from and the later one it scores
    # (src/drift.py). Reports only; it never changes a decision.
    drift: dict | None = None


def batch_drift(alerts: list[Alert], config: Config) -> dict:
    """Drift of the scored window against the training window of the same batch."""
    if len(alerts) < 10:
        return {"status": "n/a", "reason": "fewer than 10 alerts"}
    early, later, _ = split_for_training(alerts, config)
    return drift_report(early, later)


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

    flags = spike_flags(alerts, jumps, config) if alerts else []
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
        trained["training"]["queue_check"] = queue_check(
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
            training = not_promoted_training(trained["training"], "the active model scored this batch")
        split_at = None
    else:
        model = None
        escalate_threshold = review_threshold = 1.0
        label_source, model_used, train_count = "none", "rules_only", 0
        training = rules_only_training(
            "No labels yet: log alerts are ranked by severity. Metric jumps and gaps do not need labels. "
            "Mark items Real / Noise to train a model."
        )
        if trained is not None:
            training = not_promoted_training(trained["training"], "rules only (severity) scored this batch")
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
            training["test"] = class_report(alerts, proba, escalate_threshold)
    else:
        decisions = baseline_decisions(alerts, config)

    candidates: list[QueueItem] = (
        spike_items(jumps, config) + spike_items(leaks, config) + spike_items(gaps, config)
        + alert_items(alerts, decisions)
    )
    from src.optional_models import finish_queue

    shown, ml_status = finish_queue(candidates, metrics if has_metrics else None, config)
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
    shown_ids = {id(item.payload) for item in shown if item.kind != "alert"}
    known_ids = {id(spike) for spike in jumps + leaks + gaps}
    extra_shown = [
        item.payload
        for item in shown
        if item.kind != "alert" and isinstance(item.payload, Spike) and id(item.payload) not in known_ids
    ]
    _ = shadow_mode  # recorded by caller in the store
    return ScoredQueue(
        alerts=alerts,
        decisions=final,
        escalate_threshold=escalate_threshold,
        review_threshold=review_threshold,
        spike_flags=flags,
        jumps=_jump_items(
            jumps + leaks + gaps + extra_shown, alerts, final, shown_ids, config, rise_unit
        ),
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
        optional_models=ml_status,
        drift=batch_drift(alerts, config),
    )


def _train(alerts: list[Alert], metrics: pd.DataFrame | None, config: Config,
           incumbent: SavedModel | None = None) -> dict | None:
    """Fit on the early window of this batch, or None without two label classes.

    No severity stand-in labels: a model that only copies severity adds nothing
    over rules-only mode, and it used to overwrite a better saved model.
    """
    if len(alerts) < 10:
        return None
    train_alerts, test_alerts, split_at = split_for_training(alerts, config)
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
        incumbent_test = class_report(test_alerts, incumbent_proba, incumbent.escalate_threshold)
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
            "train": class_report(train_alerts, train_proba, escalate_threshold),
            "test": class_report(test_alerts, test_proba, escalate_threshold) if test_alerts else None,
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


METRIC_DRIFT_WINDOW = pd.Timedelta(hours=1)


def _recent_series_medians(metrics: pd.DataFrame | None) -> dict[str, float]:
    """Median value of each series over its last hour of points.

    Ingest jobs re-score the whole history, so a median over all of it barely
    moves; one fleet-wide number also hides a single service changing scale.
    The monitor compares these per series with earlier jobs."""
    if metrics is None or metrics.empty or not {"value", "timestamp"} <= set(metrics.columns):
        return {}
    key = "series_id" if "series_id" in metrics.columns else "service" if "service" in metrics.columns else None
    if key is None:
        return {}
    out: dict[str, float] = {}
    for series, rows in metrics.groupby(metrics[key].astype(str)):
        recent = rows[rows["timestamp"] > rows["timestamp"].max() - METRIC_DRIFT_WINDOW]["value"]
        value = recent.median()
        if pd.notna(value):
            out[series] = float(value)
    return out


def _job_meta(result: ScoredQueue, inputs: JobInputs, saved: SavedModel | None, real_marks: int,
              page: dict, whp: dict, feedback_stats: dict) -> dict:
    decisions = result.decisions
    jumps = result.jumps
    metric_median = None
    if inputs.metrics is not None and not inputs.metrics.empty and "value" in inputs.metrics.columns:
        value = inputs.metrics["value"].median()
        if pd.notna(value):
            metric_median = float(value)
    metric_medians = _recent_series_medians(inputs.metrics)

    def shown(kind: str) -> int:
        return sum(1 for j in jumps if j["kind"] == kind and j["shown"])

    meta = {
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
        "metric_medians": metric_medians,
        "label_source": result.label_source,
        "model_used": result.model_used,
        "model": saved.status(real_marks) if saved else None,
        "training": result.training,
        "pagerduty": page,
        "would_have_paged": whp,
        **feedback_stats,
    }
    prior_mean = template_prior_mean(result.alerts)
    if prior_mean is not None:
        meta["template_prior_mean"] = prior_mean
    if result.drift is not None:
        meta["drift"] = result.drift
    audit = (result.optional_models or {}).get("ranker_audit")
    if audit is not None:
        meta["ranker_audit"] = audit
    return meta


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
    page, whp = page_job(
        mode,
        shadow_mode,
        result.alerts,
        result.decisions,
        result.shown_spikes or [],
        model=result.model,
    )
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
