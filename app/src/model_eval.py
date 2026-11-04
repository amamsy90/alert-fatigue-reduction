"""Time-split comparison of the optional metric model and the learned ranker.

Variants are the current causal queue, that queue plus metric-model proposals,
that queue with the ranker choosing slots, and both. The keep rule is the same
one as the template-feature ablation: a 95% bootstrap interval on the per-fault
catch difference, and keep only when the whole interval is above zero.

GAIA is scored only when its folders are present. This module never invents
those numbers. A synthetic run is marked pending for GAIA, and it does not
turn either flag on.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

from .baseline import baseline_decisions
from .classifier import AlertClassifier
from .config import Config
from .entities import Alert, Fault
from .eval_stats import bootstrap_mean_ci
from .features import build_feature_frame
from .memory_spikes import detect_gaps, detect_jumps, detect_leaks
from .metric_model import (
    fit_metric_model,
    proposals_from_scores,
    tune_metric_threshold,
)
from .policy import bayes_threshold, spike_review_decisions, tune_threshold
from .queue import (
    QueueItem,
    alert_items,
    build_queue,
    n_days_between,
    score_queue,
    spike_items,
    tune_confirm_thresholds,
)
from .ranker import caught_fault_ids, fit_ranker

VARIANTS = ("baseline", "metric_model", "ranker", "both")


@dataclass
class EvalSpec:
    freq: str = "1min"
    window: int = 16
    stride: int = 8
    hidden: int = 8
    kernel: int = 3
    dilations: tuple[int, ...] = (1, 2, 4)
    epochs: int = 2
    seed: int = 42
    device: str = "cpu"
    backend: str = "pointwise"
    max_windows: int = 512
    budget_ks: tuple[int, ...] = (5, 10, 20, 50)
    n_draw: int = 4000


def _utc(stamp: pd.Timestamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def published_bounds(
    metrics: pd.DataFrame,
    alerts: list[Alert],
) -> tuple[pd.Timestamp, pd.Timestamp, str]:
    """Origin and exclusive stop, matching ``load_gaia`` / ``run_queue_eval``.

    The published queue starts at the first alert and stops one second after
    the last alert. Ending at the last metric point drops faults that still
    have a later alert. With no alerts, the metric span is the window.
    """
    metric_origin = _utc(metrics["timestamp"].min())
    metric_stop = _utc(metrics["timestamp"].max()) + pd.Timedelta(seconds=1)
    if not alerts:
        return metric_origin, metric_stop, "last metric"
    origin = min(_utc(alert.timestamp) for alert in alerts)
    stop = max(_utc(alert.timestamp) for alert in alerts) + pd.Timedelta(seconds=1)
    return origin, stop, "last alert"


def published_scoring_window(
    metrics: pd.DataFrame,
    alerts: list[Alert],
    faults: list[Fault],
    config: Config,
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.DataFrame, list[Fault]]:
    """Train boundary, exclusive stop, and test faults for the published GAIA split.

    The window is the first alert through one second after the last alert, the
    same cut ``evaluate_models`` and ``run_queue_eval`` use. Metric points and
    faults outside that span do not move the origin or the stop.
    """
    frame = metrics.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    origin, stop, _how = published_bounds(frame, alerts)
    boundary = origin + pd.Timedelta(days=config.split_day)
    trimmed = frame[frame["timestamp"] < stop]
    test_faults = [fault for fault in faults if boundary <= _utc(fault.start) < stop]
    return origin, boundary, stop, trimmed, test_faults


def _decision(base: set[str], cand: set[str] | None, faults: list[Fault], spec: EvalSpec, status: str) -> dict:
    if status != "scored" or cand is None:
        return {
            "status": status,
            "decision": "skipped",
            "reason": status,
            "per_fault_catch_delta": None,
        }
    if not faults:
        return {
            "status": "skipped",
            "decision": "skipped",
            "reason": "no held-out faults",
            "per_fault_catch_delta": None,
        }
    delta = [float(fault.fault_id in cand) - float(fault.fault_id in base) for fault in faults]
    interval = bootstrap_mean_ci(delta, n_draw=spec.n_draw, seed=spec.seed)
    kept = interval.low > 0
    return {
        "status": "scored",
        "decision": "keep" if kept else "drop",
        "reason": (
            "the 95% interval for the per-fault catch difference lies above zero"
            if kept
            else "the 95% interval for the per-fault catch difference does not lie above zero"
        ),
        "per_fault_catch_delta": {
            "mean": round(interval.mean, 4),
            "low": round(interval.low, 4),
            "high": round(interval.high, 4),
            "n": interval.n,
        },
    }


def _row(name: str, shown: list[QueueItem], faults: list[Fault], n_days: int, config: Config) -> dict:
    scored = score_queue(name, shown, faults, n_days, config)
    return {
        "faults_caught": scored["faults_caught"],
        "faults": scored["faults"],
        "detection_rate": scored["detection_rate"],
        "false_items_per_day": scored["false_items_per_day"],
        "items_per_day": scored["items_per_day"],
        "precision": scored["precision"],
    }


def evaluate_models(
    metrics: pd.DataFrame,
    alerts: list[Alert],
    faults: list[Fault],
    *,
    config: Config | None = None,
    spec: EvalSpec | None = None,
    dataset: str = "synthetic",
    train_end: pd.Timestamp | None = None,
    test_end: pd.Timestamp | None = None,
) -> dict:
    """Train on ``[origin, train_end)`` and score ``[train_end, test_end)``."""
    config = config or Config()
    spec = spec or EvalSpec()
    frame = metrics.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    origin, default_stop, window_end = published_bounds(frame, alerts)
    boundary = _utc(train_end) if train_end is not None else origin + pd.Timedelta(days=config.split_day)
    if test_end is not None:
        stop = _utc(test_end)
        window_end = "caller"
    else:
        stop = default_stop
    frame = frame[frame["timestamp"] < stop]
    train_alerts = [alert for alert in alerts if alert.timestamp < boundary]
    test_alerts = [alert for alert in alerts if boundary <= alert.timestamp < stop]
    train_faults = [fault for fault in faults if fault.start < boundary]
    test_faults = [fault for fault in faults if boundary <= fault.start < stop]
    n_days = n_days_between(boundary, stop, config)

    train_jumps = detect_jumps(frame, config, detect_until=boundary)
    test_jumps = detect_jumps(frame, config, detect_from=boundary, detect_until=stop)
    train_gaps = detect_gaps(frame, config, detect_until=boundary)
    test_gaps = detect_gaps(frame, config, detect_from=boundary, detect_until=stop)
    train_leaks = detect_leaks(frame, config, detect_until=boundary)
    test_leaks = detect_leaks(frame, config, detect_from=boundary, detect_until=stop)
    thr, _by_series = tune_confirm_thresholds(train_jumps, train_faults, config, train_until=boundary)
    tuned = replace(config, confirm_min_rise=thr)

    archive, metric_status = fit_metric_model(
        frame,
        train_faults,
        boundary,
        freq=spec.freq,
        window=spec.window,
        stride=spec.stride,
        hidden=spec.hidden,
        kernel=spec.kernel,
        dilations=spec.dilations,
        epochs=spec.epochs,
        seed=spec.seed,
        device=spec.device,
        max_windows=spec.max_windows,
    )
    train_scores: dict = {}
    test_scores: dict = {}
    train_proposals: list = []
    test_proposals: list = []
    if archive is not None:
        train_scores = archive.score(frame, end=boundary)
        test_scores = archive.score(frame, start=boundary, end=stop)
        metric_status = "trained"

    # One classifier fit. Slot choice still depends on K, so decisions are rebuilt per K.
    alert_model = None
    alert_threshold = None
    try:
        if train_alerts:
            alert_model = AlertClassifier(config).fit(train_alerts, train_jumps)
            alert_threshold = tune_threshold(
                train_alerts, alert_model.predict_proba(train_alerts, train_jumps), config
            )
    except ValueError:
        alert_model = None

    by_k: dict[str, dict] = {}
    for budget in spec.budget_ks:
        cfg = replace(tuned, budget_k=budget)
        if alert_model is not None and alert_threshold is not None and test_alerts:
            proba = alert_model.predict_proba(test_alerts, test_jumps)
            flags = (
                [bool(value) for value in build_feature_frame(test_alerts, cfg, test_jumps)["during_memory_spike"]]
                if test_jumps
                else [False] * len(test_alerts)
            )
            test_decisions = spike_review_decisions(
                test_alerts, proba, cfg, alert_threshold, bayes_threshold(cfg), flags
            )
            train_proba = alert_model.predict_proba(train_alerts, train_jumps)
            train_flags = (
                [bool(value) for value in build_feature_frame(train_alerts, cfg, train_jumps)["during_memory_spike"]]
                if train_jumps
                else [False] * len(train_alerts)
            )
            train_decisions = spike_review_decisions(
                train_alerts, train_proba, cfg, alert_threshold, bayes_threshold(cfg), train_flags
            )
        else:
            test_decisions = baseline_decisions(test_alerts, cfg)
            train_decisions = baseline_decisions(train_alerts, cfg)

        train_items = (
            spike_items(train_jumps, cfg)
            + spike_items(train_leaks, cfg)
            + spike_items(train_gaps, cfg)
            + alert_items(train_alerts, train_decisions)
        )
        test_items = (
            spike_items(test_jumps, cfg)
            + spike_items(test_leaks, cfg)
            + spike_items(test_gaps, cfg)
            + alert_items(test_alerts, test_decisions)
        )
        if archive is not None:
            archive.threshold = tune_metric_threshold(
                train_scores, train_items, train_faults, cfg, freq=spec.freq, stride=spec.stride
            )
            train_proposals = proposals_from_scores(
                train_scores, archive.threshold, train_items, freq=spec.freq, stride=spec.stride
            )
            test_proposals = proposals_from_scores(
                test_scores, archive.threshold, test_items, freq=spec.freq, stride=spec.stride
            )
        train_extra = [QueueItem(spike.shown_at, spike.kind, spike.service, spike) for spike in train_proposals]
        test_extra = [QueueItem(spike.shown_at, spike.kind, spike.service, spike) for spike in test_proposals]

        plain, plain_status = fit_ranker(
            train_items,
            train_faults,
            cfg,
            None,
            train_end=boundary,
            backend=spec.backend,
            device=spec.device,
            seed=spec.seed,
        )
        both_ranker = None
        both_status = "metric-model-skipped"
        if archive is not None:
            both_ranker, both_status = fit_ranker(
                train_items + train_extra,
                train_faults,
                cfg,
                train_scores,
                train_end=boundary,
                backend=spec.backend,
                device=spec.device,
                seed=spec.seed,
            )

        baseline_queue = build_queue(test_items, cfg)
        shown: dict[str, list[QueueItem] | None] = {
            "baseline": baseline_queue,
            "metric_model": build_queue(test_items + test_extra, cfg) if archive is not None else None,
            "ranker": plain.select(test_items, None, cfg) if plain is not None else None,
            "both": both_ranker.select(test_items + test_extra, test_scores, cfg) if both_ranker is not None else None,
        }
        base_caught = caught_fault_ids(baseline_queue, test_faults)
        block: dict[str, dict] = {}
        for name in VARIANTS:
            queue = shown[name]
            if name == "baseline":
                entry = _row(name, baseline_queue, test_faults, n_days, cfg)
                entry["decision"] = "baseline"
                entry["status"] = "scored"
                entry["caught_ids"] = sorted(base_caught)
            else:
                reason = "scored"
                if queue is None and name == "metric_model":
                    reason = metric_status
                elif queue is None and name == "ranker":
                    reason = plain_status
                elif queue is None and name == "both":
                    reason = both_status if archive is not None else metric_status
                if queue is None:
                    entry = {}
                    caught = None
                else:
                    entry = _row(name, queue, test_faults, n_days, cfg)
                    caught = caught_fault_ids(queue, test_faults)
                    entry["caught_ids"] = sorted(caught)
                entry.update(_decision(base_caught, caught, test_faults, spec, "scored" if queue is not None else reason))
            block[name] = entry
        by_k[str(budget)] = block

    headline = by_k.get(str(config.budget_k), by_k.get("20", next(iter(by_k.values()))))
    return {
        "dataset": dataset,
        "gaia": "pending: this run did not score GAIA" if dataset != "gaia" else "scored",
        "split": "first week train, later days test" if train_end is None else "walk-forward: train on the past only",
        "split_day": config.split_day,
        "train_end": boundary.isoformat(),
        "test_end": stop.isoformat(),
        "window_end": window_end,
        "test_fault_ids": [fault.fault_id for fault in test_faults],
        "test_faults": len(test_faults),
        "flags_default": "off",
        "flags_changed": False,
        "flags_reason": (
            "A synthetic or partial run does not turn METRIC_MODEL or LEARNED_RANKER on. "
            "Both stay off until a GAIA run at K=20 has a 95% interval entirely above zero."
        ),
        "metric_model": {
            "status": metric_status,
            "device": None if archive is None else archive.device,
            "device_requested": None if archive is None else archive.device_requested,
            "fell_back": None if archive is None else archive.fell_back,
            "channels": [] if archive is None else list(archive.channels),
            "normal_only": None if archive is None else archive.normal_only,
            "versions": {} if archive is None else archive.versions,
            "pages": False,
        },
        "ranker": {"backend": spec.backend, "device": spec.device, "pages": False},
        "by_k": by_k,
        "headline_k": config.budget_k if str(config.budget_k) in by_k else int(next(iter(by_k))),
        "headline": headline,
    }


def walk_forward_models(
    metrics: pd.DataFrame,
    alerts: list[Alert],
    faults: list[Fault],
    *,
    config: Config | None = None,
    spec: EvalSpec | None = None,
    dataset: str = "synthetic",
) -> dict:
    """Refit on everything before each later week, then score that week at K=20."""
    config = replace(config or Config(), budget_k=20)
    spec = spec or EvalSpec()
    week = EvalSpec(**{**spec.__dict__, "budget_ks": (20,)})
    frame = metrics.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    origin, end, _how = published_bounds(frame, alerts)
    test_start = origin + pd.Timedelta(days=config.split_day)
    folds = []
    while test_start < end:
        test_stop = min(test_start + pd.Timedelta(days=7), end)
        in_week = [fault for fault in faults if test_start <= fault.start < test_stop]
        if not in_week:
            break
        report = evaluate_models(
            frame,
            alerts,
            faults,
            config=config,
            spec=week,
            dataset=dataset,
            train_end=test_start,
            test_end=test_stop,
        )
        folds.append(
            {
                "train_end": report["train_end"],
                "test_end": report["test_end"],
                "test_faults": report["test_faults"],
                "test_fault_ids": report["test_fault_ids"],
                "metric_model_status": report["metric_model"]["status"],
                "by_k": report["by_k"],
            }
        )
        test_start = test_stop
    return {
        "dataset": dataset,
        "gaia": "pending: this run did not score GAIA" if dataset != "gaia" else "scored",
        "folds": folds,
        "pooled": _pooled_verdict(folds, spec),
        "flags_changed": False,
    }


def _pooled_verdict(folds: list[dict], spec: EvalSpec) -> dict:
    """One keep/drop for every test fault in every fold, not one per fold.

    Each fault contributes one catch difference. The 95% interval is the same
    bootstrap as a single split. A fold that did not score a variant skips
    that variant's pool.
    """
    verdicts: dict[str, dict] = {}
    for name in ("metric_model", "ranker", "both"):
        deltas: list[float] = []
        skipped: str | None = None
        for fold in folds:
            block = fold["by_k"].get("20") or next(iter(fold["by_k"].values()))
            row = block[name]
            if row.get("decision") == "skipped" or "caught_ids" not in row:
                skipped = str(row.get("reason") or row.get("status") or "skipped")
                break
            base = set(block["baseline"].get("caught_ids") or [])
            caught = set(row["caught_ids"])
            for fault_id in fold["test_fault_ids"]:
                deltas.append(float(fault_id in caught) - float(fault_id in base))
        if skipped is not None or not deltas:
            verdicts[name] = {
                "status": skipped or "no held-out faults",
                "decision": "skipped",
                "reason": skipped or "no held-out faults",
                "per_fault_catch_delta": None,
            }
            continue
        interval = bootstrap_mean_ci(deltas, n_draw=spec.n_draw, seed=spec.seed)
        kept = interval.low > 0
        verdicts[name] = {
            "status": "scored",
            "decision": "keep" if kept else "drop",
            "reason": (
                "the 95% interval for the pooled per-fault catch difference lies above zero"
                if kept
                else "the 95% interval for the pooled per-fault catch difference does not lie above zero"
            ),
            "per_fault_catch_delta": {
                "mean": round(interval.mean, 4),
                "low": round(interval.low, 4),
                "high": round(interval.high, 4),
                "n": interval.n,
            },
        }
    return verdicts
