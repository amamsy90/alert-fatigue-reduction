"""The policy spends the budget only on alerts that are worth escalating."""
import pandas as pd

from src.config import Config
from src.entities import Action, Alert
from src.policy import (
    bayes_threshold,
    policy_decisions,
    review_decisions,
    share_budget_with_jumps,
    spike_review_decisions,
    tune_threshold,
)

T0 = pd.Timestamp("2026-01-05 09:00:00", tz="UTC")


def _alert(aid: str, minute: float, message: str, fault_id: str | None = None):
    return Alert(
        alert_id=aid,
        timestamp=T0 + pd.Timedelta(minutes=minute),
        source="db-01",
        alert_type="cpu",
        severity="warning",
        message=message,
        fault_id=fault_id,
    )


def test_bayes_threshold_matches_ten_to_one_cost():
    assert bayes_threshold(Config()) == 1 / 11


def test_threshold_keeps_only_the_useful_prefix():
    alerts = [
        _alert("p1", 0, "real one", "f1"),
        _alert("p2", 1, "real two", "f2"),
        _alert("n1", 2, "noise one"),
        _alert("n2", 3, "noise two"),
    ]
    threshold = tune_threshold(alerts, [0.9, 0.8, 0.2, 0.1], Config())
    assert threshold == 0.8


def test_budget_and_threshold_limit_escalations():
    config = Config(budget_k=1)
    alerts = [
        _alert("high", 0, "alpha"),
        _alert("dup", 1, "alpha"),
        _alert("mid", 2, "beta"),
        _alert("low", 3, "gamma"),
    ]
    decisions = {
        d.alert_id: d
        for d in policy_decisions(alerts, [0.9, 0.95, 0.7, 0.1], config, threshold=0.5)
    }
    assert decisions["dup"].action is Action.SUPPRESS
    assert decisions["dup"].reason == "exact duplicate"
    assert decisions["low"].reason == "below cost threshold"
    escalated = [aid for aid, d in decisions.items() if d.action is Action.ESCALATE]
    assert escalated == ["high"]
    assert decisions["mid"].reason == "below daily budget"


def test_uncertain_alerts_fill_only_leftover_slots():
    config = Config(budget_k=1)
    alerts = [
        _alert("sure", 0, "alpha"),
        _alert("unsure", 1, "beta"),
        _alert("noise", 2, "gamma"),
    ]
    scores = [0.9, 0.4, 0.05]
    tight = {
        d.alert_id: d
        for d in review_decisions(alerts, scores, config, escalate_threshold=0.5, review_threshold=0.2)
    }
    assert tight["sure"].action is Action.ESCALATE
    assert tight["unsure"].reason == "below daily budget"
    assert tight["noise"].reason == "confident noise"

    wider = Config(budget_k=2)
    opened = {
        d.alert_id: d
        for d in review_decisions(alerts, scores, wider, escalate_threshold=0.5, review_threshold=0.2)
    }
    assert opened["sure"].action is Action.ESCALATE
    assert opened["unsure"].action is Action.REVIEW


def test_jumps_take_the_first_slots_and_escalations_beat_asks():
    from src.entities import Decision

    config = Config(budget_k=3)
    alerts = [_alert("ask", 0, "a"), _alert("esc_low", 1, "b"), _alert("esc_high", 2, "c")]
    decisions = [
        Decision("ask", Action.REVIEW, 0.9, ""),
        Decision("esc_low", Action.ESCALATE, 0.6, ""),
        Decision("esc_high", Action.ESCALATE, 0.8, ""),
    ]
    day = T0.normalize()
    shared = {
        d.alert_id: d for d in share_budget_with_jumps(alerts, decisions, {day: 2}, config)
    }
    assert shared["esc_high"].action is Action.ESCALATE
    assert shared["esc_low"].reason == "daily budget used by memory jumps"
    assert shared["ask"].action is Action.SUPPRESS

    crowded = share_budget_with_jumps(alerts, decisions, {day: 7}, config)
    assert all(d.action is Action.SUPPRESS for d in crowded)


def test_only_alerts_inside_a_memory_jump_are_asked():
    config = Config(budget_k=3)
    alerts = [
        _alert("sure", 0, "alpha"),
        _alert("jump", 1, "beta"),
        _alert("calm", 2, "gamma"),
        _alert("jump_low", 3, "delta"),
    ]
    scores = [0.9, 0.3, 0.3, 0.01]
    in_spike = [False, True, False, True]
    decisions = {
        d.alert_id: d
        for d in spike_review_decisions(
            alerts, scores, config, escalate_threshold=0.5, review_threshold=0.2, in_spike=in_spike
        )
    }
    assert decisions["sure"].action is Action.ESCALATE
    assert decisions["jump"].action is Action.REVIEW
    assert decisions["calm"].action is Action.SUPPRESS
    assert decisions["jump_low"].action is Action.SUPPRESS
