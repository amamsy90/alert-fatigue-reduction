"""Score a batch into the on-call queue: metric jumps, monitoring gaps, log alerts.

The queue is causal (src/queue.py): items appear when a live system could know
them, first come first served, at most K a day. With no labels it runs on rules
only (metric jumps + gaps + alert severity); with labels or on-call marks the
alert classifier takes over the alert share of the budget.
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, replace
import threading
from pathlib import Path

import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config, day_of
from src.entities import SURFACED_ACTIONS, Action, Alert, Decision, Fault
from src.features import build_feature_frame
from src.gaia_loader import load_run_folder
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.memory_spikes import Spike, detect_gaps, detect_jumps, detect_leaks, load_metric_folder
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold
from src.queue import QueueItem, alert_items, build_queue, spike_items, tune_confirm_thresholds
from src.sample_data import generate_demo_day

from .model_store import SavedModel, load_model, save_model
from .settings import config_from_env, live_detector
from .store import QueueStore

APP_ROOT = Path(__file__).resolve().parent.parent


def _default_paths() -> dict[str, Path]:
    return {
        "gaia_run": APP_ROOT / "data" / "gaia_raw" / "run" / "run",
        "gaia_metrics": APP_ROOT / "data" / "gaia_metrics",
        "sample_dir": APP_ROOT / "data" / "sample_product",
    }


def load_alerts_from_dicts(rows: list[dict]) -> list[Alert]:
    alerts: list[Alert] = []
    for i, row in enumerate(rows):
        ts = pd.to_datetime(row["timestamp"], utc=True)
        alerts.append(
            Alert(
                alert_id=str(row.get("alert_id") or f"w{i:06d}"),
                timestamp=ts,
                source=str(row["source"]),
                alert_type=str(row.get("alert_type") or row.get("severity") or "info"),
                severity=str(row.get("severity") or "info").lower(),
                message=str(row.get("message") or "")[:300],
                fault_id=row.get("fault_id"),
            )
        )
    alerts.sort(key=lambda a: a.timestamp)
    return alerts


def load_metrics_csv(path: str | Path) -> pd.DataFrame:
    """Accept either a folder of GAIA-style files or one CSV with timestamp,value,service."""
    path = Path(path)
    if path.is_dir():
        return load_metric_folder(path)
    # Any column names: timestamp/time/date, value/count/..., series/service/host/...;
    # long or wide (one column per series). See src/generic.py.
    from src.generic import load_metrics

    frame, _ = load_metrics(path, default_series=path.stem)
    return frame


def _spike_flags(alerts: list[Alert], spikes: list, config: Config) -> list[bool]:
    if not spikes:
        return [False] * len(alerts)
    frame = build_feature_frame(alerts, config, spikes)
    return [bool(v) for v in frame["during_memory_spike"]]


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


FEEDBACK_PREFIX = "fb_"


def apply_feedback(alerts: list[Alert], store: QueueStore) -> dict[str, int]:
    """Label alerts from on-call marks: inside a Real window is a fault, and an
    alert marked Noise is noise even inside a window. Existing labels are kept."""
    faults = [
        Fault(
            fault_id=f["fault_id"],
            start=pd.Timestamp(f["start"]),
            end=pd.Timestamp(f["end"]),
            component=f["component"],
        )
        for f in store.feedback_faults()
    ]
    noise = store.feedback_noise_alerts()
    labelled = overridden = 0
    by_service: dict[str, list[Fault]] = defaultdict(list)
    for fault in faults:
        by_service[fault.component].append(fault)
    for alert in alerts:
        if (alert.source, alert.timestamp.value) in noise:
            if alert.fault_id is not None:
                overridden += 1
            alert.fault_id = None
            continue
        if alert.fault_id is None:
            for fault in by_service.get(alert.source, []):
                if fault.start <= alert.timestamp <= fault.end:
                    alert.fault_id = fault.fault_id
                    labelled += 1
                    break
    return {"labelled_by_real_marks": labelled, "cleared_by_noise_marks": overridden}


def _label_source(alerts: list[Alert]) -> str:
    kinds = set()
    for alert in alerts:
        if alert.fault_id is None:
            continue
        if alert.fault_id == "weak":
            kinds.add("weak_severity")
        elif str(alert.fault_id).startswith(FEEDBACK_PREFIX):
            kinds.add("feedback")
        else:
            kinds.add("real")
    if "weak_severity" in kinds:
        return "weak_severity"
    return "+".join(sorted(kinds)) or "none"


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
    # Jumps and gaps the queue showed (what PagerDuty would page).
    shown_spikes: list | None = None
    config: Config | None = None
    rise_unit: str = "GB"
    detector_label: str = "built-in memory-jump rule"
    # Per-series confirm rise learned on the early window only. Empty if there
    # was no held-out window (the one-day sample) or no labels.
    confirm_by_series: dict | None = None


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
) -> ScoredQueue:
    """Score EVERY alert in the batch and build the day-by-day causal queue.

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
    gaps = detect_gaps(metrics, config) if has_metrics else []
    # Leaks only make sense for byte metrics (the rise is absolute) and the built-in rule.
    leaks = (
        detect_leaks(metrics, config)
        if has_metrics and config.memory_jump_mode == "absolute" and detector is None
        else []
    )

    trained = _train(alerts, metrics if has_metrics else None, config)
    prefer_saved = saved is not None and saved.label_source != "weak_severity" and trained is None
    if trained is not None:
        model = trained["model"]
        escalate_threshold = trained["escalate_threshold"]
        review_threshold = trained["review_threshold"]
        label_source = trained["label_source"]
        model_used, train_count = "trained", trained["train_alerts"]
        training = trained["training"]
        split_at = trained["split_at"]
    elif prefer_saved:
        model = saved.model
        escalate_threshold = saved.escalate_threshold
        review_threshold = saved.review_threshold
        label_source = saved.label_source
        model_used, train_count = "saved", saved.train_alerts
        training = {"model_used": "saved", "note": "This batch had too few labels, so the last saved model scored it.",
                    "threshold": round(float(escalate_threshold), 4), "train": None, "test": None}
        split_at = None
    else:
        model = None
        escalate_threshold = review_threshold = 1.0
        label_source, model_used, train_count = "none", "rules_only", 0
        training = _rules_only_training(
            "No labels yet: log alerts are ranked by severity. Metric jumps and gaps do not need labels. "
            "Mark items Real / Noise to train a model."
        )
        split_at = None

    flags = [bool(v) for v in build_feature_frame(alerts, config, jumps)["during_memory_spike"]] if alerts else []
    if model is not None and alerts:
        proba = model.predict_proba(alerts, jumps)
        decisions = spike_review_decisions(alerts, proba, config, escalate_threshold, review_threshold, flags)
        if model_used == "saved":
            training["test"] = _class_report(alerts, proba, escalate_threshold)
    else:
        decisions = baseline_decisions(alerts, config)

    confirm_by_series: dict[str, float] = {}
    # Learn the confirm gate on the early window only, and only when a later
    # window exists. The one-day sample has no later window, so it stays on the
    # built-in rule and is not fit on the same points it shows.
    metric_split = split_at
    if metric_split is None and has_metrics and faults:
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
        model=model if model_used == "trained" else None,
        train_alerts=train_count,
        training=training,
        shown_spikes=[item.payload for item in shown if item.kind != "alert"],
        config=config,
        rise_unit=rise_unit,
        detector_label=(detector.label() if detector is not None else "built-in memory-jump rule"),
        confirm_by_series=confirm_by_series,
    )


def _train(alerts: list[Alert], metrics: pd.DataFrame | None, config: Config) -> dict | None:
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
    test_proba = model.predict_proba(test_alerts, test_spikes) if test_alerts else []
    return {
        "model": model,
        "escalate_threshold": escalate_threshold,
        "review_threshold": bayes_threshold(config),
        "label_source": _label_source(train_alerts),
        "train_alerts": len(train_alerts),
        "split_at": split_at,
        "training": {
            "model_used": "trained",
            "note": (
                "Fit on the early window; the threshold is frozen before the later window. "
                "Every alert is in the queue, but only the later window is an honest test."
            ),
            "threshold": round(float(escalate_threshold), 4),
            "train": _class_report(train_alerts, train_proba, escalate_threshold),
            "test": _class_report(test_alerts, test_proba, escalate_threshold) if test_alerts else None,
        },
    }


# One job at a time: an Alertmanager retry and a click on "Run" must not score
# the same buffer twice (double rows, and in Live mode double pages).
_JOB_LOCK = threading.Lock()

# Jobs kept in the database (each ingest job stores 14 days of jumps). Marks
# are copied into the feedback table, so pruning old jobs never loses a label.
DEFAULT_JOB_RETENTION = 200


def run_job(*args, **kwargs) -> dict:
    with _JOB_LOCK:
        return _run_job_locked(*args, **kwargs)


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
    if shadow_mode is None:
        from .ops import get_shadow_mode

        shadow_mode = get_shadow_mode()

    paths = _default_paths()
    metrics: pd.DataFrame | None = None
    faults: list[Fault] | None = None
    source = mode

    sample_config = None
    if mode == "sample":
        generate_demo_day(paths["sample_dir"])
        alerts = load_alerts(paths["sample_dir"] / "alerts.csv")
        faults = load_faults(paths["sample_dir"] / "faults.csv")
        alerts, faults = align_alerts_to_faults(alerts, faults)
        metrics = load_metrics_csv(paths["sample_dir"] / "metrics.csv")
        # Keep the leak window from the environment (0 turns leaks off) but
        # score this byte series with the absolute 0.5 GB rule it was built for.
        sample_config = replace(
            config_from_env(budget_k),
            memory_jump_mode="absolute",
            memory_jump_bytes=Config().memory_jump_bytes,
        )
    elif mode == "gaia":
        run_path = paths["gaia_run"]
        if not run_path.exists():
            raise FileNotFoundError(f"GAIA run folder not found: {run_path}")
        alerts, faults = load_run_folder(run_path)
        metrics_dir = paths["gaia_metrics"]
        if metrics_dir.exists():
            metrics = load_metric_folder(metrics_dir)
    elif mode == "upload":
        from src.generic import load_events, load_incidents

        if alerts_csv is None and alert_rows is None and not metrics_path:
            raise ValueError("upload needs an alerts/events file, a metrics file, or both")
        if metrics_path:
            metrics = load_metrics_csv(metrics_path)
        known = set(metrics["series_id"].astype(str)) if metrics is not None else None
        if alert_rows is not None:
            alerts = load_alerts_from_dicts(alert_rows)
        elif alerts_csv is not None:
            alerts, _ = load_events(alerts_csv, known)
        else:
            alerts = []
        if faults_path:
            faults, _ = load_incidents(faults_path, known_series=known)
            alerts, faults = align_alerts_to_faults(alerts, faults)
        source = "upload"
    elif mode == "ingest":
        if not alert_rows:
            raise ValueError("ingest mode needs alert_rows from the buffer")
        alerts = load_alerts_from_dicts(alert_rows)
        if metrics_path:
            metrics = load_metrics_csv(metrics_path)
        else:
            from .ops import get_prom_url
            from .prometheus_adapter import fetch_memory_metrics

            if get_prom_url():
                metrics = fetch_memory_metrics()
        source = "ingest"
    else:
        raise ValueError(f"Unknown mode: {mode}")

    feedback_stats = apply_feedback(alerts, store)
    saved = load_model()
    result = score_alerts(
        alerts,
        metrics=metrics,
        budget_k=budget_k,
        shadow_mode=shadow_mode,
        saved=saved,
        faults=faults,
        config=sample_config,
        use_saved_detector=mode != "sample",
    )
    real_marks = store.feedback_summary()["real"]
    if result.model is not None:
        saved = SavedModel(
            model=result.model,
            escalate_threshold=result.escalate_threshold,
            review_threshold=result.review_threshold,
            label_source=result.label_source,
            trained_at=pd.Timestamp.now("UTC").isoformat(),
            train_alerts=result.train_alerts,
            feedback_real=real_marks,
        )
        save_model(saved)
    decisions = result.decisions
    # Hard safety: sample / GAIA / Evaluate-upload never page, even if Live is on.
    # Only ingest (live webhook) jobs may call notify_escalations when Live + key.
    from .ops import PAGEABLE_MODES, can_page, configured_channels, page_cutoff, set_page_watermark
    from .notifiers import notify_opsgenie, notify_slack
    from .pagerduty import notify_escalations, page_items, would_have_paged

    # Ingest jobs re-score the metric history; only items shown since the last
    # ingest job are new. Alerts in an ingest batch are all new (the buffer is
    # acknowledged after each job), so they are not filtered.
    page_spikes = list(result.shown_spikes or [])
    run_at = pd.Timestamp.now(tz="UTC")
    if mode in PAGEABLE_MODES:
        cutoff = page_cutoff(run_at)
        page_spikes = [s for s in page_spikes if cutoff < s.shown_at <= run_at]

    if mode not in PAGEABLE_MODES:
        page = {"sent": 0, "failed": 0, "skipped": f"mode={mode} never pages"}
    elif shadow_mode:
        page = {"sent": 0, "failed": 0, "skipped": "shadow mode"}
    elif not configured_channels():
        page = {"sent": 0, "failed": 0, "skipped": "no channel configured"}
    elif not can_page():
        page = {"sent": 0, "failed": 0, "skipped": "shadow mode"}
    else:
        channels = configured_channels()
        items = page_items(result.alerts, decisions, spikes=page_spikes)
        per: dict[str, dict] = {}
        if "pagerduty" in channels:
            per["pagerduty"] = notify_escalations(result.alerts, decisions, spikes=page_spikes)
        if "slack" in channels:
            per["slack"] = notify_slack(items, len(items))
        if "opsgenie" in channels:
            per["opsgenie"] = notify_opsgenie(items)
        page = {
            "sent": sum(int(r.get("sent", 0)) for r in per.values()),
            "failed": sum(int(r.get("failed", 0)) for r in per.values()),
            "channels": per,
        }
    whp = would_have_paged(result.alerts, decisions, spikes=page_spikes)
    if mode in PAGEABLE_MODES:
        whp["since"] = cutoff.isoformat()
        # Advance in shadow too, so turning Live on never pages the shadow period.
        # Keep the old mark if a send failed, so the next job retries it.
        if not page.get("failed"):
            set_page_watermark(run_at)
    job_id = store.save_job(
        source=source,
        budget_k=budget_k,
        escalate_threshold=result.escalate_threshold,
        review_threshold=result.review_threshold,
        shadow_mode=shadow_mode,
        alerts=result.alerts,
        decisions=decisions,
        spike_flags=result.spike_flags,
        jumps=result.jumps,
        day_offset_hours=result.config.day_offset_hours if result.config else 0.0,
        meta={
            "alert_count": len(result.alerts),
            "escalate": sum(1 for d in decisions if d.action.value == "escalate"),
            "review": sum(1 for d in decisions if d.action.value == "review"),
            "suppress": sum(1 for d in decisions if d.action.value == "suppress"),
            "jumps": sum(1 for j in result.jumps if j["kind"] == "jump"),
            "jumps_shown": sum(1 for j in result.jumps if j["kind"] == "jump" and j["shown"]),
            "gap_items": sum(1 for j in result.jumps if j["kind"] == "gap" and j["shown"]),
            "leak_items": sum(1 for j in result.jumps if j["kind"] == "leak" and j["shown"]),
            "rise_unit": result.rise_unit,
            "detector": result.detector_label,
            "confirm_by_series": result.confirm_by_series or {},
            "confirm_min_rise": None if result.config is None else result.config.confirm_min_rise,
            "has_metrics": metrics is not None and not metrics.empty,
            "label_source": result.label_source,
            "model_used": result.model_used,
            "model": saved.status(real_marks) if saved else None,
            "training": result.training,
            "pagerduty": page,
            "would_have_paged": whp,
            **feedback_stats,
        },
    )
    try:
        keep = int(os.environ.get("JOB_RETENTION", DEFAULT_JOB_RETENTION))
    except ValueError:
        keep = DEFAULT_JOB_RETENTION
    if keep > 0:
        store.prune_jobs(keep=keep)
    return {
        "job_id": job_id,
        "source": source,
        "budget_k": budget_k,
        "shadow_mode": shadow_mode,
        "escalate_threshold": result.escalate_threshold,
        "review_threshold": result.review_threshold,
        "days": store.list_days(job_id),
        "meta": store.job_meta(job_id)["meta"],
    }
