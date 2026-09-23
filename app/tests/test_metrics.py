"""Sanity tests for the metric modules."""
import pandas as pd
import pytest

from src.entities import Action, Alert, Decision, Fault
from src.metrics import (
    alert_reduction_rate,
    classification_pr,
    cost_weighted_error,
    incident_detection_rate,
    mtta_minutes,
)

T0 = pd.Timestamp("2026-01-05", tz="UTC")


def make_alert(aid, minutes, fault_id=None, severity="high"):
    return Alert(
        alert_id=aid,
        timestamp=T0 + pd.Timedelta(minutes=minutes),
        source="db-01",
        alert_type="cpu",
        severity=severity,
        message="m",
        fault_id=fault_id,
    )


@pytest.fixture
def scenario():
    alerts = [
        make_alert("a1", 5, fault_id="f1"),
        make_alert("a2", 10, fault_id="f1"),
        make_alert("a3", 20),  # noise
    ]
    faults = [Fault("f1", T0, T0 + pd.Timedelta(hours=1), "db-01")]
    decisions = [
        Decision("a1", Action.ESCALATE, 1.0, "test"),
        Decision("a2", Action.SUPPRESS, 1.0, "dup"),
        Decision("a3", Action.SUPPRESS, 1.0, "noise"),
    ]
    return alerts, faults, decisions


def test_classification_pr(scenario):
    alerts, faults, decisions = scenario
    pr = classification_pr(decisions, alerts)
    assert pr["precision"] == 1.0  # the one surfaced alert is a real fault
    assert pr["recall"] == 0.5  # 1 of 2 fault alerts surfaced


def test_reduction_rate(scenario):
    alerts, faults, decisions = scenario
    assert alert_reduction_rate(len(alerts), decisions) == pytest.approx(2 / 3)


def test_detection_rate(scenario):
    alerts, faults, decisions = scenario
    assert incident_detection_rate(decisions, alerts, faults) == 1.0


def test_mtta(scenario):
    alerts, faults, decisions = scenario
    assert mtta_minutes(decisions, alerts, faults) == 5.0


def test_cost_weighted(scenario):
    alerts, faults, decisions = scenario
    # 1 missed fault alert x 10, 0 false alarms
    assert cost_weighted_error(decisions, alerts, miss_cost=10) == 10.0


def test_detection_zero_when_all_suppressed(scenario):
    alerts, faults, _ = scenario
    decisions = [Decision(a.alert_id, Action.SUPPRESS, 1.0, "x") for a in alerts]
    assert incident_detection_rate(decisions, alerts, faults) == 0.0
    assert mtta_minutes(decisions, alerts, faults) is None
