"""Run classify + memory spikes + review policy into queue decisions."""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.classifier import AlertClassifier
from src.config import Config
from src.entities import Action, Alert, Decision, Fault
from src.features import build_feature_frame
from src.gaia_loader import load_run_folder
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.memory_spikes import Spike, detect_jumps, load_metric_folder
from src.policy import (
    bayes_threshold,
    share_budget_with_jumps,
    spike_review_decisions,
    tune_threshold,
)
from src.sample_data import generate

from .model_store import SavedModel, load_model, save_model
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
    frame = pd.read_csv(path)
    if "service" not in frame.columns:
        raise ValueError("Memory CSV needs a service column (or use a GAIA metrics folder)")
    raw = frame["timestamp"]
    if pd.api.types.is_numeric_dtype(raw) and float(raw.max()) > 1e12:
        timestamps = pd.to_datetime(raw, unit="ms", utc=True)
    else:
        timestamps = pd.to_datetime(raw, utc=True)
    out = pd.DataFrame(
        {
            "timestamp": timestamps,
            "value": frame["value"].astype(float),
            "service": frame["service"].astype(str),
            "series_id": frame["service"].astype(str),
        }
    )
    return out.drop_duplicates(["series_id", "timestamp"]).sort_values("timestamp")


def _spike_flags(alerts: list[Alert], spikes: list, config: Config) -> list[bool]:
    if not spikes:
        return [False] * len(alerts)
    frame = build_feature_frame(alerts, config, spikes)
    return [bool(v) for v in frame["during_memory_spike"]]


def _jump_items(
    spikes: list[Spike], alerts: list[Alert], decisions: list[Decision]
) -> list[dict]:
    """One queue item per memory jump, with the alerts that fell inside it.

    A jump with no alerts inside still matters: it may be a fault the logs missed.
    """
    by_service: dict[str, list[tuple[Alert, Decision]]] = defaultdict(list)
    for alert, decision in zip(alerts, decisions):
        by_service[alert.source].append((alert, decision))
    items = []
    for spike in spikes:
        inside = [
            decision
            for alert, decision in by_service.get(spike.service, [])
            if spike.start <= alert.timestamp <= spike.end
        ]
        items.append(
            {
                "day": spike.start.normalize().date().isoformat(),
                "service": spike.service,
                "start": spike.start.isoformat(),
                "end": spike.end.isoformat(),
                "rise_gb": round(spike.peak_rise / 1e9, 3),
                "alerts_inside": len(inside),
                "escalated_inside": sum(d.action == Action.ESCALATE for d in inside),
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
        if (alert.source, alert.timestamp.isoformat()) in noise:
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


def score_alerts(
    alerts: list[Alert],
    *,
    metrics: pd.DataFrame | None = None,
    budget_k: int = 20,
    shadow_mode: bool = True,
    saved: SavedModel | None = None,
) -> ScoredQueue:
    """Fit on the early window and score the later one; fall back to `saved`.

    The saved model is used when this batch cannot be trained on (too few alerts
    or one label class), or when it could only be trained on the severity
    stand-in while the saved model learned from real or confirmed labels.
    """
    config = Config(budget_k=budget_k)
    has_metrics = metrics is not None and not metrics.empty
    trained = _train(alerts, metrics if has_metrics else None, config)
    prefer_saved = saved is not None and saved.label_source != "weak_severity" and (
        trained is None or trained["label_source"] == "weak_severity"
    )
    if prefer_saved:
        score_alerts_list = alerts
        # Memory values must be bytes: the jump size is an absolute rise.
        score_spikes = detect_jumps(metrics, config) if has_metrics else []
        model = saved.model
        escalate_threshold = saved.escalate_threshold
        review_threshold = saved.review_threshold
        label_source = saved.label_source
        model_used, train_count = "saved", saved.train_alerts
    elif trained is not None:
        score_alerts_list = trained["score_alerts"]
        score_spikes = trained["score_spikes"]
        model = trained["model"]
        escalate_threshold = trained["escalate_threshold"]
        review_threshold = trained["review_threshold"]
        label_source = trained["label_source"]
        model_used, train_count = "trained", trained["train_alerts"]
    else:
        raise ValueError(
            "Cannot train on this batch (need 10+ alerts with both fault-like and "
            "noise examples) and no saved model exists yet"
        )

    proba = model.predict_proba(score_alerts_list, score_spikes)
    flags = _spike_flags(score_alerts_list, score_spikes, config)
    decisions = spike_review_decisions(
        score_alerts_list, proba, config, escalate_threshold, review_threshold, flags
    )
    jumps_per_day: dict[pd.Timestamp, int] = defaultdict(int)
    for spike in score_spikes:
        jumps_per_day[spike.start.normalize()] += 1
    decisions = share_budget_with_jumps(score_alerts_list, decisions, jumps_per_day, config)
    _ = shadow_mode  # recorded by caller in the store
    return ScoredQueue(
        alerts=score_alerts_list,
        decisions=decisions,
        escalate_threshold=escalate_threshold,
        review_threshold=review_threshold,
        spike_flags=flags,
        jumps=_jump_items(score_spikes, score_alerts_list, decisions),
        label_source=label_source,
        model_used=model_used,
        model=model if model_used == "trained" else None,
        train_alerts=train_count,
    )


def _train(alerts: list[Alert], metrics: pd.DataFrame | None, config: Config) -> dict | None:
    """Fit on this batch, or None when it has too few alerts or labels to learn from."""
    if len(alerts) < 10:
        return None
    train_alerts, test_alerts, split_at = _split(alerts, config)
    if len({a.fault_id is not None for a in train_alerts}) < 2:
        for a in train_alerts:
            if a.severity in ("critical", "error", "high") and a.fault_id is None:
                a.fault_id = "weak"
        if len({a.fault_id is not None for a in train_alerts}) < 2:
            return None
    train_spikes: list = []
    test_spikes: list = []
    if metrics is not None:
        train_spikes = detect_jumps(metrics, config, detect_until=split_at)
        test_spikes = detect_jumps(metrics, config, detect_from=split_at)
    model = AlertClassifier(config).fit(train_alerts, train_spikes)
    train_proba = model.predict_proba(train_alerts, train_spikes)
    return {
        "model": model,
        "escalate_threshold": tune_threshold(train_alerts, train_proba, config),
        "review_threshold": bayes_threshold(config),
        "label_source": _label_source(train_alerts),
        "train_alerts": len(train_alerts),
        "score_alerts": test_alerts if test_alerts else alerts,
        "score_spikes": test_spikes if test_alerts else train_spikes + test_spikes,
    }


def run_job(
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
        shadow_mode = os.environ.get("SHADOW_MODE", "1") != "0"

    paths = _default_paths()
    metrics: pd.DataFrame | None = None
    source = mode

    if mode == "sample":
        generate(paths["sample_dir"], seed=Config().random_seed)
        alerts = load_alerts(paths["sample_dir"] / "alerts.csv")
        faults = load_faults(paths["sample_dir"] / "faults.csv")
        alerts, _ = align_alerts_to_faults(alerts, faults)
    elif mode == "gaia":
        run_path = paths["gaia_run"]
        if not run_path.exists():
            raise FileNotFoundError(f"GAIA run folder not found: {run_path}")
        alerts, _faults = load_run_folder(run_path)
        metrics_dir = paths["gaia_metrics"]
        if metrics_dir.exists():
            metrics = load_metric_folder(metrics_dir)
    elif mode == "upload":
        if alerts_csv is None and alert_rows is None:
            raise ValueError("upload mode needs alerts_csv or alert_rows")
        if alert_rows is not None:
            alerts = load_alerts_from_dicts(alert_rows)
        else:
            alerts = load_alerts(alerts_csv)
        if metrics_path:
            metrics = load_metrics_csv(metrics_path)
        if faults_path:
            alerts, _ = align_alerts_to_faults(alerts, load_faults(faults_path))
        source = "upload"
    elif mode == "ingest":
        if not alert_rows:
            raise ValueError("ingest mode needs alert_rows from the buffer")
        alerts = load_alerts_from_dicts(alert_rows)
        if metrics_path:
            metrics = load_metrics_csv(metrics_path)
        elif os.environ.get("PROM_URL"):
            from .prometheus_adapter import fetch_memory_metrics

            metrics = fetch_memory_metrics()
        source = "ingest"
    else:
        raise ValueError(f"Unknown mode: {mode}")

    feedback_stats = apply_feedback(alerts, store)
    saved = load_model()
    result = score_alerts(
        alerts, metrics=metrics, budget_k=budget_k, shadow_mode=shadow_mode, saved=saved
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
    page = {"sent": 0, "failed": 0, "skipped": "shadow mode"}
    if not shadow_mode:
        from .pagerduty import notify_escalations

        page = notify_escalations(result.alerts, decisions)
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
        meta={
            "alert_count": len(result.alerts),
            "escalate": sum(1 for d in decisions if d.action.value == "escalate"),
            "review": sum(1 for d in decisions if d.action.value == "review"),
            "suppress": sum(1 for d in decisions if d.action.value == "suppress"),
            "jumps": len(result.jumps),
            "has_metrics": metrics is not None and not metrics.empty,
            "label_source": result.label_source,
            "model_used": result.model_used,
            "model": saved.status(real_marks) if saved else None,
            "pagerduty": page,
            **feedback_stats,
        },
    )
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
