"""Baseline: fixed severity thresholds + exact duplicate suppression.

This is the reference approach the project must beat (design spec 4.6).
Escalate an alert if its severity passes the threshold and an identical
alert (same source, type and message) has not been escalated before.
When the daily budget forces a choice, the most severe alerts win.
"""
from __future__ import annotations

import pandas as pd

from .config import Config, day_of
from .entities import Action, Alert, Decision


def baseline_decisions(alerts: list[Alert], config: Config) -> list[Decision]:
    decisions: list[Decision] = []
    seen_exact: set[tuple[str, str, str]] = set()
    # Per-day list of escalation candidates: (severity rank, timestamp, index)
    candidates_by_day: dict[pd.Timestamp, list[tuple[int, pd.Timestamp, int]]] = {}

    for idx, alert in enumerate(alerts):
        key = (alert.source, alert.alert_type, alert.message)
        is_dup = key in seen_exact
        passes_threshold = alert.severity in config.escalate_severities

        if is_dup:
            decisions.append(
                Decision(alert.alert_id, Action.SUPPRESS, 1.0, "exact duplicate")
            )
            continue
        if not passes_threshold:
            decisions.append(
                Decision(
                    alert.alert_id,
                    Action.SUPPRESS,
                    1.0,
                    f"severity '{alert.severity}' below threshold",
                )
            )
            continue

        seen_exact.add(key)
        day = day_of(alert.timestamp, config)
        rank = config.severity_rank.get(alert.severity, 0)
        candidates_by_day.setdefault(day, []).append((rank, alert.timestamp, idx))
        decisions.append(
            Decision(alert.alert_id, Action.ESCALATE, 1.0, "severity threshold")
        )

    # Apply the daily budget: keep top-K per day by (severity rank, earliest)
    for day, cands in candidates_by_day.items():
        if len(cands) <= config.budget_k:
            continue
        cands.sort(key=lambda c: (-c[0], c[1]))
        overflow = {idx for _, _, idx in cands[config.budget_k:]}
        for idx in overflow:
            d = decisions[idx]
            decisions[idx] = Decision(
                d.alert_id,
                Action.SUPPRESS,
                d.confidence,
                "daily budget exhausted",
            )
    return decisions
