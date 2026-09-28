"""Budget-aware escalate / suppress policy (design spec section 4.5).

An alert is eligible when escalating it costs less than missing it:
P(real) >= false_alarm_cost / (miss_cost + false_alarm_cost). The threshold
actually used is the one that minimizes cost on the training week, so a
miscalibrated score cannot spend the budget on noise. At most K eligible
alerts are escalated each day, first come, first served: alerts are read in
time order, so no decision depends on an alert that arrives later that day.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from .baseline import time_order
from .config import Config, day_of
from .entities import Action, Alert, Decision


def bayes_threshold(config: Config) -> float:
    """Escalate when the expected cost of a miss exceeds a false alarm."""
    return config.false_alarm_cost / (config.miss_cost + config.false_alarm_cost)


def tune_threshold(alerts: list[Alert], proba: np.ndarray, config: Config) -> float:
    """Lowest score at which a prefix of the ranking still reduces train cost.

    Returns a score no training alert can reach when escalating nothing is cheaper.
    """
    labels = np.array([alert.fault_id is not None for alert in alerts])
    proba = np.asarray(proba, dtype=float)
    order = np.argsort(-proba, kind="mergesort")
    cost = float(config.miss_cost * labels.sum())
    best_cost = cost
    best_threshold = float(proba.max()) + 1e-6 if len(proba) else 1.0
    for idx in order:
        if labels[idx]:
            cost -= config.miss_cost
        else:
            cost += config.false_alarm_cost
        if cost < best_cost:
            best_cost = cost
            best_threshold = float(proba[idx])
    return best_threshold


def policy_decisions(
    alerts: list[Alert], proba: np.ndarray, config: Config, threshold: float
) -> list[Decision]:
    """Suppress duplicates and cheap alerts, then spend the daily budget in arrival order."""
    decisions: list[Decision | None] = [None] * len(alerts)
    seen: set[tuple[str, str, str]] = set()
    used: dict[pd.Timestamp, int] = defaultdict(int)

    for idx in time_order(alerts):
        alert = alerts[idx]
        score = float(proba[idx])
        key = (alert.source, alert.alert_type, alert.message)
        if key in seen:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "exact duplicate")
            continue
        seen.add(key)
        if score < threshold:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "below cost threshold")
            continue
        day = day_of(alert.timestamp, config)
        if used[day] >= config.budget_k:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "below daily budget")
            continue
        used[day] += 1
        decisions[idx] = Decision(alert.alert_id, Action.ESCALATE, score, "policy top-k")
    return decisions  # type: ignore[return-value]


def review_decisions(
    alerts: list[Alert],
    proba: np.ndarray,
    config: Config,
    escalate_threshold: float,
    review_threshold: float,
) -> list[Decision]:
    """Confident faults are escalated, uncertain alerts go to a person, both from
    the same daily budget, first come first served.

    Scores below the review floor are suppressed without asking anyone.
    """
    decisions: list[Decision | None] = [None] * len(alerts)
    seen: set[tuple[str, str, str]] = set()
    used: dict[pd.Timestamp, int] = defaultdict(int)

    for idx in time_order(alerts):
        alert = alerts[idx]
        score = float(proba[idx])
        key = (alert.source, alert.alert_type, alert.message)
        if key in seen:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "exact duplicate")
            continue
        seen.add(key)
        if score < review_threshold:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "confident noise")
            continue
        day = day_of(alert.timestamp, config)
        if used[day] >= config.budget_k:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "below daily budget")
            continue
        used[day] += 1
        if score >= escalate_threshold:
            decisions[idx] = Decision(alert.alert_id, Action.ESCALATE, score, "confident fault")
        else:
            decisions[idx] = Decision(alert.alert_id, Action.REVIEW, score, "ask a person")
    return decisions  # type: ignore[return-value]


def spike_review_decisions(
    alerts: list[Alert],
    proba: np.ndarray,
    config: Config,
    escalate_threshold: float,
    review_threshold: float,
    in_spike: list[bool],
) -> list[Decision]:
    """Confident alerts are escalated; uncertain ones go to a person only when
    they arrive inside a memory jump. One daily budget, first come first served.

    Uncertain alerts outside a jump are not worth a person's time: on GAIA they
    filled the budget with noise.
    """
    decisions: list[Decision | None] = [None] * len(alerts)
    seen: set[tuple[str, str, str]] = set()
    used: dict[pd.Timestamp, int] = defaultdict(int)

    for idx in time_order(alerts):
        alert = alerts[idx]
        score = float(proba[idx])
        key = (alert.source, alert.alert_type, alert.message)
        if key in seen:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "exact duplicate")
            continue
        seen.add(key)
        day = day_of(alert.timestamp, config)
        confident = score >= escalate_threshold
        worth_asking = in_spike[idx] and score >= review_threshold
        if not (confident or worth_asking):
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "below cost threshold")
            continue
        if used[day] >= config.budget_k:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "below daily budget")
            continue
        used[day] += 1
        if confident:
            decisions[idx] = Decision(alert.alert_id, Action.ESCALATE, score, "policy top-k")
        else:
            decisions[idx] = Decision(
                alert.alert_id, Action.REVIEW, score, "ask a person: inside memory jump"
            )
    return decisions  # type: ignore[return-value]


def share_budget_with_jumps(
    alerts: list[Alert],
    decisions: list[Decision],
    jumps_per_day: dict[pd.Timestamp, int],
    config: Config,
) -> list[Decision]:
    """DEPRECATED (hindsight): reserves slots for every jump of the day before the
    day has happened. The live queue is src.queue.build_queue; this is kept only
    so old notebooks still run.
    """
    order = {Action.ESCALATE: 0, Action.REVIEW: 1}
    by_day: dict[pd.Timestamp, list[tuple[int, float, int]]] = defaultdict(list)
    for idx, (alert, decision) in enumerate(zip(alerts, decisions)):
        if decision.action in order:
            by_day[day_of(alert.timestamp, config)].append(
                (order[decision.action], -float(decision.confidence), idx)
            )
    shared = list(decisions)
    for day, surfaced in by_day.items():
        slots = max(0, config.budget_k - min(config.budget_k, jumps_per_day.get(day, 0)))
        surfaced.sort()
        for _, _, idx in surfaced[slots:]:
            shared[idx] = Decision(
                decisions[idx].alert_id,
                Action.SUPPRESS,
                decisions[idx].confidence,
                "daily budget used by memory jumps",
            )
    return shared


def meets_gate(policy: dict, baseline: dict) -> bool:
    """Detection ahead, MTTA at least 50% lower, false alerts at least 80% lower.

    A baseline that catches nothing has no MTTA; then any policy MTTA passes
    (there is nothing faster to compare with) as long as detection is ahead.
    """
    detection_ok = policy["detection_rate"] > baseline["detection_rate"]
    policy_mtta = policy["mtta_minutes"]
    base_mtta = baseline["mtta_minutes"]
    mtta_ok = policy_mtta is not None and (base_mtta is None or policy_mtta <= 0.5 * base_mtta)
    false_ok = policy["false_alerts_per_day"] <= 0.2 * baseline["false_alerts_per_day"]
    return bool(detection_ok and mtta_ok and false_ok)
