"""Metric modules (design spec section 4.6).

All metrics compare decisions against GAIA's injected-fault ground truth.
Conventions:
- "surfaced" = the alert was shown to a human (escalate / group / review)
- an alert is a true positive if it is surfaced AND has a fault_id label
- a fault is "detected" if at least one of its alerts was surfaced
"""
from __future__ import annotations

import pandas as pd

from .entities import SURFACED_ACTIONS, Alert, Decision, Fault


def _surfaced_ids(decisions: list[Decision]) -> set[str]:
    return {d.alert_id for d in decisions if d.action in SURFACED_ACTIONS}


def classification_pr(
    decisions: list[Decision], alerts: list[Alert]
) -> dict[str, float]:
    """Precision/recall of surfacing, at the alert level (criterion 1)."""
    truth = {a.alert_id: a.fault_id is not None for a in alerts}
    surfaced = _surfaced_ids(decisions)
    tp = sum(1 for aid in surfaced if truth.get(aid, False))
    fp = sum(1 for aid in surfaced if not truth.get(aid, False))
    fn = sum(1 for aid, is_fault in truth.items() if is_fault and aid not in surfaced)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": precision, "recall": recall, "tp": tp, "fp": fp, "fn": fn}


def alert_reduction_rate(n_raw: int, decisions: list[Decision]) -> float:
    """1 - surfaced/raw (criterion 3, system effectiveness)."""
    if n_raw == 0:
        return 0.0
    return 1.0 - len(_surfaced_ids(decisions)) / n_raw


def incident_detection_rate(
    decisions: list[Decision], alerts: list[Alert], faults: list[Fault]
) -> float:
    """Fraction of injected faults with >= 1 surfaced alert (criterion 3)."""
    if not faults:
        return 0.0
    surfaced = _surfaced_ids(decisions)
    fault_by_alert = {a.alert_id: a.fault_id for a in alerts}
    detected = {
        fault_by_alert[aid] for aid in surfaced if fault_by_alert.get(aid)
    }
    return len(detected) / len(faults)


def mtta_minutes(
    decisions: list[Decision], alerts: list[Alert], faults: list[Fault]
) -> float | None:
    """Mean time from fault injection to first surfaced alert (criterion 4).

    Undetected faults are excluded (reported separately by detection rate).
    Returns None if no fault was detected.
    """
    surfaced = _surfaced_ids(decisions)
    ts_by_alert = {a.alert_id: a.timestamp for a in alerts}
    fault_by_alert = {a.alert_id: a.fault_id for a in alerts}
    first_surfaced: dict[str, pd.Timestamp] = {}
    for aid in surfaced:
        fid = fault_by_alert.get(aid)
        if not fid:
            continue
        ts = ts_by_alert[aid]
        if fid not in first_surfaced or ts < first_surfaced[fid]:
            first_surfaced[fid] = ts
    delays = []
    for fault in faults:
        if fault.fault_id in first_surfaced:
            delays.append(
                (first_surfaced[fault.fault_id] - fault.start).total_seconds() / 60.0
            )
    return sum(delays) / len(delays) if delays else None


def false_alert_burden_per_day(
    decisions: list[Decision], alerts: list[Alert]
) -> float:
    """False alerts surfaced per day, averaged over the stream (criterion 4)."""
    truth = {a.alert_id: a.fault_id is not None for a in alerts}
    ts_by_alert = {a.alert_id: a.timestamp for a in alerts}
    surfaced = _surfaced_ids(decisions)
    if not surfaced:
        return 0.0
    days = max(
        1,
        (max(ts_by_alert[a] for a in surfaced) - min(ts_by_alert[a] for a in surfaced)).days
        + 1,
    )
    false_surfaced = sum(1 for aid in surfaced if not truth.get(aid, False))
    return false_surfaced / days


def cost_weighted_error(
    decisions: list[Decision],
    alerts: list[Alert],
    miss_cost: float = 10.0,
    false_alarm_cost: float = 1.0,
) -> float:
    """miss_cost x missed fault alerts + false_alarm_cost x false surfacings."""
    truth = {a.alert_id: a.fault_id is not None for a in alerts}
    surfaced = _surfaced_ids(decisions)
    misses = sum(
        1 for aid, is_fault in truth.items() if is_fault and aid not in surfaced
    )
    false_alarms = sum(1 for aid in surfaced if not truth.get(aid, False))
    return miss_cost * misses + false_alarm_cost * false_alarms


def evaluate_all(
    name: str,
    decisions: list[Decision],
    alerts: list[Alert],
    faults: list[Fault],
    miss_cost: float = 10.0,
) -> dict:
    """One-row summary of every M1 metric for system or baseline."""
    pr = classification_pr(decisions, alerts)
    mtta = mtta_minutes(decisions, alerts, faults)
    return {
        "system": name,
        "alerts": len(alerts),
        "surfaced": len(_surfaced_ids(decisions)),
        "precision": round(pr["precision"], 4),
        "recall": round(pr["recall"], 4),
        "reduction_rate": round(alert_reduction_rate(len(alerts), decisions), 4),
        "detection_rate": round(incident_detection_rate(decisions, alerts, faults), 4),
        "mtta_minutes": round(mtta, 2) if mtta is not None else None,
        "false_alerts_per_day": round(false_alert_burden_per_day(decisions, alerts), 2),
        "cost_weighted_error": round(
            cost_weighted_error(decisions, alerts, miss_cost), 1
        ),
    }
