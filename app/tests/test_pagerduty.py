"""PagerDuty is silent in shadow mode and only pages escalations."""
from unittest.mock import patch

import pandas as pd

from product.pagerduty import notify_escalations
from src.entities import Action, Alert, Decision

T0 = pd.Timestamp("2026-01-12 10:00:00", tz="UTC")


def _pair(action: Action, aid: str = "a1"):
    alert = Alert(aid, T0, "webservice1", "HighMemory", "warning", "working set jumped")
    return alert, Decision(aid, action, 0.9, "policy")


class _Resp:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_no_key_sends_nothing():
    alert, decision = _pair(Action.ESCALATE)
    out = notify_escalations([alert], [decision], key="")
    assert out["sent"] == 0
    assert out["skipped"]


def test_only_escalations_are_sent():
    pairs = [
        _pair(Action.ESCALATE, "e1"),
        _pair(Action.REVIEW, "r1"),
        _pair(Action.SUPPRESS, "s1"),
        _pair(Action.ESCALATE, "e2"),
    ]
    alerts, decisions = zip(*pairs)
    with patch("product.pagerduty.urlopen", return_value=_Resp()) as mocked:
        out = notify_escalations(list(alerts), list(decisions), key="RKEY")
    assert out["sent"] == 2 and out["failed"] == 0
    assert mocked.call_count == 2
