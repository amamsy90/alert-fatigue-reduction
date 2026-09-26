"""Budget-aware escalate / suppress policy (design spec section 4.5).

An alert is eligible when escalating it costs less than missing it:
P(real) >= false_alarm_cost / (miss_cost + false_alarm_cost). The threshold
actually used is the one that minimizes cost on the training week, so a
miscalibrated score cannot spend the budget on noise. Eligible alerts are
ranked by expected miss cost, and at most K of them are escalated each day.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

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
    """Suppress duplicates and cheap alerts, then spend the daily budget."""
    decisions: list[Decision | None] = [None] * len(alerts)
    seen: set[tuple[str, str, str]] = set()
    candidates: dict[pd.Timestamp, list[tuple[float, int]]] = defaultdict(list)

    for idx, alert in enumerate(alerts):
        score = float(proba[idx])
        key = (alert.source, alert.alert_type, alert.message)
        if key in seen:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "exact duplicate")
            continue
        seen.add(key)
        if score < threshold:
            decisions[idx] = Decision(
                alert.alert_id, Action.SUPPRESS, score, "below cost threshold"
            )
            continue
        expected_miss = score * config.miss_cost
        candidates[day_of(alert.timestamp, config)].append((expected_miss, idx))

    chosen: set[int] = set()
    for day_candidates in candidates.values():
        day_candidates.sort(key=lambda item: -item[0])
        for _, idx in day_candidates[: config.budget_k]:
            chosen.add(idx)

    for idx, alert in enumerate(alerts):
        if decisions[idx] is not None:
            continue
        score = float(proba[idx])
        if idx in chosen:
            decisions[idx] = Decision(alert.alert_id, Action.ESCALATE, score, "policy top-k")
        else:
            decisions[idx] = Decision(
                alert.alert_id, Action.SUPPRESS, score, "below daily budget"
            )
    return decisions  # type: ignore[return-value]


def review_decisions(
    alerts: list[Alert],
    proba: np.ndarray,
    config: Config,
    escalate_threshold: float,
    review_threshold: float,
) -> list[Decision]:
    """Confident faults take budget slots first. Uncertain alerts fill the rest.

    Scores below the review floor are suppressed without asking anyone.
    """
    decisions: list[Decision | None] = [None] * len(alerts)
    seen: set[tuple[str, str, str]] = set()
    escalate_by_day: dict[pd.Timestamp, list[tuple[float, int]]] = defaultdict(list)
    review_by_day: dict[pd.Timestamp, list[tuple[float, int]]] = defaultdict(list)

    for idx, alert in enumerate(alerts):
        score = float(proba[idx])
        key = (alert.source, alert.alert_type, alert.message)
        if key in seen:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "exact duplicate")
            continue
        seen.add(key)
        day = day_of(alert.timestamp, config)
        if score >= escalate_threshold:
            escalate_by_day[day].append((score, idx))
        elif score >= review_threshold:
            review_by_day[day].append((score, idx))
        else:
            decisions[idx] = Decision(alert.alert_id, Action.SUPPRESS, score, "confident noise")

    chosen_escalate: set[int] = set()
    chosen_review: set[int] = set()
    days = set(escalate_by_day) | set(review_by_day)
    for day in days:
        slots = config.budget_k
        ranked = sorted(escalate_by_day.get(day, []), key=lambda item: -item[0])
        for _, idx in ranked[:slots]:
            chosen_escalate.add(idx)
        slots -= min(slots, len(ranked))
        ranked_review = sorted(review_by_day.get(day, []), key=lambda item: -item[0])
        for _, idx in ranked_review[:slots]:
            chosen_review.add(idx)

    for idx, alert in enumerate(alerts):
        if decisions[idx] is not None:
            continue
        score = float(proba[idx])
        if idx in chosen_escalate:
            decisions[idx] = Decision(alert.alert_id, Action.ESCALATE, score, "confident fault")
        elif idx in chosen_review:
            decisions[idx] = Decision(alert.alert_id, Action.REVIEW, score, "ask a person")
        else:
            decisions[idx] = Decision(
                alert.alert_id, Action.SUPPRESS, score, "below daily budget"
            )
    return decisions  # type: ignore[return-value]


def spike_review_decisions(
    alerts: list[Alert],
    proba: np.ndarray,
    config: Config,
    escalate_threshold: float,
    review_threshold: float,
    in_spike: list[bool],
) -> list[Decision]:
    """Cost policy first; leftover slots go only to alerts inside a memory jump.

    Uncertain alerts outside a jump are not worth a person's time: on GAIA they
    filled the budget with noise.
    """
    decisions = policy_decisions(alerts, proba, config, escalate_threshold)
    used: dict[pd.Timestamp, int] = defaultdict(int)
    for alert, decision in zip(alerts, decisions):
        if decision.action == Action.ESCALATE:
            used[day_of(alert.timestamp, config)] += 1

    candidates: dict[pd.Timestamp, list[tuple[float, int]]] = defaultdict(list)
    for idx, (alert, decision) in enumerate(zip(alerts, decisions)):
        if (
            decision.action == Action.SUPPRESS
            and decision.reason != "exact duplicate"
            and in_spike[idx]
            and float(proba[idx]) >= review_threshold
        ):
            candidates[day_of(alert.timestamp, config)].append((float(proba[idx]), idx))

    for day, day_candidates in candidates.items():
        slots = max(0, config.budget_k - used[day])
        day_candidates.sort(key=lambda item: -item[0])
        for score, idx in day_candidates[:slots]:
            decisions[idx] = Decision(
                alerts[idx].alert_id, Action.REVIEW, score, "ask a person: inside memory jump"
            )
    return decisions


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
