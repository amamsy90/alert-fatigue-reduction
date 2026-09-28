"""GAIA ingestion and alert-to-fault alignment (design spec section 4.1).

Expected input folder layout (adapt COLUMN_MAP below if the real GAIA
files use different column names):

    data/gaia/
        alerts.csv   # one row per alert/event
        faults.csv   # one row per injected fault (ground truth)

alerts.csv columns (after mapping): alert_id, timestamp, source, alert_type,
severity, message
faults.csv columns (after mapping): fault_id, start, end, component,
description
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .entities import Alert, Fault

# Map our canonical names -> column names in your export. The defaults match the
# CSVs the CLI and web app accept; edit the right-hand side for other exports.
ALERT_COLUMN_MAP = {
    "alert_id": "alert_id",
    "timestamp": "timestamp",
    "source": "source",
    "alert_type": "alert_type",
    "severity": "severity",
    "message": "message",
}
FAULT_COLUMN_MAP = {
    "fault_id": "fault_id",
    "start": "start",
    "end": "end",
    "component": "component",
    "description": "description",
}


def load_alerts(path: str | Path) -> list[Alert]:
    df = pd.read_csv(path)
    df = df.rename(columns={v: k for k, v in ALERT_COLUMN_MAP.items()})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return [
        Alert(
            alert_id=str(r.alert_id),
            timestamp=r.timestamp,
            source=str(r.source),
            alert_type=str(r.alert_type),
            severity=str(r.severity).lower(),
            message=str(r.message),
        )
        for r in df.itertuples()
    ]


def load_faults(path: str | Path) -> list[Fault]:
    df = pd.read_csv(path)
    df = df.rename(columns={v: k for k, v in FAULT_COLUMN_MAP.items()})
    df["start"] = pd.to_datetime(df["start"], utc=True)
    df["end"] = pd.to_datetime(df["end"], utc=True)
    return [
        Fault(
            fault_id=str(r.fault_id),
            start=r.start,
            end=r.end,
            component=str(r.component),
            description=str(getattr(r, "description", "")),
        )
        for r in df.itertuples()
    ]


def align_alerts_to_faults(
    alerts: list[Alert], faults: list[Fault]
) -> tuple[list[Alert], list[Fault]]:
    """Label each alert with the fault it belongs to (time window + component).

    Alerts outside every fault window are labelled noise (fault_id=None). A
    fault with component "" or "*" matches any source. The first fault in
    `faults` order that covers an alert wins.
    """
    wildcard = [f for f in faults if f.component in ("", "*")]
    by_service: dict[str, list[Fault]] = {}
    for fault in faults:
        if fault.component not in ("", "*"):
            by_service.setdefault(fault.component, []).append(fault)
    order = {id(f): i for i, f in enumerate(faults)}
    for alert in alerts:
        best = None
        for fault in wildcard + by_service.get(alert.source, []):
            if fault.start <= alert.timestamp <= fault.end and (
                best is None or order[id(fault)] < order[id(best)]
            ):
                best = fault
        if best is not None:
            alert.fault_id = best.fault_id
    return alerts, faults


def time_split(
    items: list, split_day: int
) -> tuple[list, list]:
    """Split timestamped items into train (week 1) / test (week 2).

    Items must have a .timestamp (alerts) or .start (faults) attribute.
    """
    if not items:
        return [], []
    def when(x) -> pd.Timestamp:
        return x.timestamp if hasattr(x, "timestamp") else x.start

    t0 = min(when(x) for x in items)
    boundary = t0 + pd.Timedelta(days=split_day)
    train: list = []
    test: list = []
    for x in items:
        ts = getattr(x, "timestamp", getattr(x, "start", None))
        (train if ts < boundary else test).append(x)
    return train, test
