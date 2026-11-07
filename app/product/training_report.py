"""Small helpers that split a labelled batch and summarise a fit for the job report."""
from __future__ import annotations

from collections import defaultdict

import pandas as pd

from src.config import Config
from src.entities import Alert, Fault
from src.ingestion import time_split


def split_for_training(alerts: list[Alert], config: Config) -> tuple[list, list, pd.Timestamp]:
    origin = min(a.timestamp for a in alerts)
    split_at = origin + pd.Timedelta(days=config.split_day)
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    if len(train_alerts) < 5 or len({a.fault_id is not None for a in train_alerts}) < 2:
        # Labels are thin in the first week: train on the first half instead.
        split_at = origin + (max(a.timestamp for a in alerts) - origin) * 0.5
        train_alerts = [a for a in alerts if a.timestamp < split_at]
        test_alerts = [a for a in alerts if a.timestamp >= split_at]
    return train_alerts, test_alerts, split_at


def class_report(alerts: list[Alert], proba, threshold: float) -> dict:
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


def rules_only_training(note: str) -> dict:
    return {"model_used": "rules_only", "note": note, "train": None, "test": None, "threshold": None}


def not_promoted_training(report: dict, fallback: str) -> dict:
    """The fit's held-out report, marked as a candidate that did not score the batch."""
    out = dict(report)
    out["model_used"] = "candidate"
    out["note"] = (
        f"A model was fit on the early window but did not pass the promotion gate ({report.get('gate')}), "
        f"so it is kept as a candidate and {fallback}."
    )
    return out


def faults_from_labels(alerts: list[Alert]) -> list[Fault]:
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
