"""Translate Prometheus Alertmanager webhooks into the queue's alert rows."""
from __future__ import annotations

import pandas as pd


def looks_like_alertmanager(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        return bool(payload.get("version") or payload.get("receiver"))
    first = alerts[0]
    return isinstance(first, dict) and ("startsAt" in first or "labels" in first)


def from_alertmanager(payload: dict, *, include_resolved: bool = False) -> list[dict]:
    """Map one Alertmanager notification to `{timestamp, source, severity, ...}` rows.

    Firing alerts become rows. Resolved alerts are skipped unless `include_resolved`.
    Service is taken from labels in this order: service, app, container, job, instance.
    """
    rows: list[dict] = []
    for item in payload.get("alerts") or []:
        if not include_resolved and str(item.get("status", "firing")).lower() == "resolved":
            continue
        labels = item.get("labels") or {}
        notes = item.get("annotations") or {}
        source = (
            labels.get("service")
            or labels.get("app")
            or labels.get("container")
            or labels.get("job")
            or labels.get("instance")
            or "unknown"
        )
        severity = str(labels.get("severity") or "warning").lower()
        name = labels.get("alertname") or "alert"
        message = notes.get("description") or notes.get("summary") or name
        started = item.get("startsAt")
        if not started:
            continue
        rows.append(
            {
                "alert_id": item.get("fingerprint") or f"{source}:{name}:{started}",
                "timestamp": started,
                "source": str(source),
                "alert_type": str(name),
                "severity": severity,
                "message": str(message)[:300],
            }
        )
    return rows


def normalize_ingest(payload: object) -> list[dict]:
    """Accept either our `{alerts: [...]}` body or a raw Alertmanager webhook."""
    if looks_like_alertmanager(payload):
        return from_alertmanager(payload)  # type: ignore[arg-type]
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict) and isinstance(payload.get("alerts"), list):
        return list(payload["alerts"])
    raise ValueError("Body must be {alerts: [...]} or an Alertmanager webhook")


def validate_rows(rows: list) -> tuple[list[dict], list[dict]]:
    """Split alert rows into usable ones and rejections with a reason.

    A usable row is an object with a parseable `timestamp` and a non-empty
    `source`. Bad rows must never reach the buffer: one of them used to make
    every later scoring job fail ("poison message").
    """
    good: list[dict] = []
    errors: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append({"index": i, "reason": "not an object"})
            continue
        source = row.get("source")
        if not isinstance(source, str) or not source.strip():
            errors.append({"index": i, "reason": "missing source"})
            continue
        raw = row.get("timestamp")
        try:
            ts = pd.to_datetime(raw, utc=True) if raw not in (None, "") else pd.NaT
        except (ValueError, TypeError, OverflowError):
            ts = pd.NaT
        if pd.isna(ts):
            errors.append({"index": i, "reason": f"unreadable timestamp {str(raw)[:40]!r}"})
            continue
        good.append(row)
    return good, errors
