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


def test_shown_jumps_and_gaps_are_paged_with_stable_keys():
    from product.pagerduty import spike_event
    from src.memory_spikes import Spike

    jump = Spike("web", "webservice1", T0, T0 + pd.Timedelta(minutes=5), 1.4e9,
                 confirm=T0 + pd.Timedelta(seconds=30), rise_at_confirm=1.1e9)
    gap = Spike("gap", "dbservice1,webservice1", T0, T0 + pd.Timedelta(hours=2),
                confirm=T0 + pd.Timedelta(minutes=10), kind="gap")
    sent = []
    with patch("product.pagerduty.urlopen", side_effect=lambda req, timeout: (sent.append(req), _Resp())[1]):
        out = notify_escalations([], [], key="k", spikes=[jump, gap])
    assert out["sent"] == 2
    assert spike_event(jump, "k")["dedup_key"] == spike_event(jump, "k")["dedup_key"]
    assert "1.10 GB" in spike_event(jump, "k")["payload"]["summary"]
    assert "No metrics" in spike_event(gap, "k")["payload"]["summary"]
