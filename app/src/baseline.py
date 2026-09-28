"""Baseline: fixed severity thresholds + exact duplicate suppression.

This is the reference approach the project must beat (design spec 4.6).
Escalate an alert if its severity passes the threshold and an identical
alert (same source, type and message) has not been escalated before.
The daily budget is spent first come, first served: alerts are read in time
order and an alert is never dropped because of one that arrives later.
"""
from __future__ import annotations

from collections import defaultdict

import pandas as pd

from .config import Config, day_of
from .entities import Action, Alert, Decision


def time_order(alerts: list[Alert]) -> list[int]:
    """Indices in arrival order (ties keep input order): how a live system reads them."""
    return sorted(range(len(alerts)), key=lambda i: (alerts[i].timestamp, i))


def baseline_decisions(alerts: list[Alert], config: Config) -> list[Decision]:
    decisions: list[Decision | None] = [None] * len(alerts)
    seen_exact: set[tuple[str, str, str]] = set()
    used: dict[pd.Timestamp, int] = defaultdict(int)

    for idx in time_order(alerts):
        alert = alerts[idx]
        key = (alert.source, alert.alert_type, alert.message)
        if key in seen_exact:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, 1.0, "exact duplicate")
            continue
        if alert.severity not in config.escalate_severities:
            decisions[idx] = Decision(
                alert.alert_id, Action.SUPPRESS, 1.0, f"severity '{alert.severity}' below threshold"
            )
            continue
        seen_exact.add(key)
        day = day_of(alert.timestamp, config)
        if used[day] >= config.budget_k:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, 1.0, "daily budget exhausted")
            continue
        used[day] += 1
        decisions[idx] = Decision(alert.alert_id, Action.ESCALATE, 1.0, "severity threshold")
    return decisions  # type: ignore[return-value]
