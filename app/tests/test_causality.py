"""No decision may depend on data from later than the moment it is made."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.baseline import baseline_decisions
from src.config import Config
from src.entities import Action, Alert
from src.memory_spikes import Spike, detect_gaps, detect_leaks, jump_scores
from src.policy import policy_decisions, spike_review_decisions
from src.queue import QueueItem, build_queue

T0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")


def _alert(i: int, minutes: float, severity: str = "error") -> Alert:
    return Alert(f"a{i}", T0 + pd.Timedelta(minutes=minutes), f"svc{i}", "log", severity, f"msg {i}")


def test_baseline_keeps_early_alerts_when_severe_ones_arrive_later():
    cfg = Config(budget_k=2)
    alerts = [_alert(0, 10, "error"), _alert(1, 20, "error"), _alert(2, 30, "critical"), _alert(3, 40, "critical")]
    actions = [d.action for d in baseline_decisions(alerts, cfg)]
    assert actions == [Action.ESCALATE, Action.ESCALATE, Action.SUPPRESS, Action.SUPPRESS]


def test_baseline_decision_does_not_change_when_later_alerts_are_added():
    cfg = Config(budget_k=3)
    early = [_alert(i, 10 * i, "error") for i in range(3)]
    later = [_alert(10 + i, 200 + i, "critical") for i in range(5)]
    alone = baseline_decisions(early, cfg)
    with_later = baseline_decisions(early + later, cfg)[:3]
    assert [d.action for d in alone] == [d.action for d in with_later]


def test_policy_reads_alerts_in_time_order_even_if_input_is_not():
    cfg = Config(budget_k=1)
    alerts = [_alert(0, 50), _alert(1, 10)]  # input order is not time order
    proba = np.array([0.99, 0.6])
    actions = [d.action for d in policy_decisions(alerts, proba, cfg, threshold=0.5)]
    assert actions == [Action.SUPPRESS, Action.ESCALATE]  # the earlier one got the slot


def test_spike_review_shares_one_budget_first_come():
    cfg = Config(budget_k=1)
    alerts = [_alert(0, 10), _alert(1, 20)]
    proba = np.array([0.3, 0.99])  # first: uncertain but inside a jump; second: confident
    out = spike_review_decisions(alerts, proba, cfg, 0.9, 0.2, [True, False])
    assert [d.action for d in out] == [Action.REVIEW, Action.SUPPRESS]


def _series(values: list[float], step_s: int = 30) -> pd.Series:
    idx = pd.date_range(T0, periods=len(values), freq=f"{step_s}s")
    return pd.Series(values, index=idx, dtype=float)


def test_robust_scores_do_not_see_later_points():
    rng = np.random.default_rng(0)
    base = list(100 + rng.normal(0, 1, 200))
    cfg = Config(memory_jump_mode="robust", memory_jump_window_minutes=15)
    early, _ = jump_scores(_series(base), cfg)
    with_outlier, _ = jump_scores(_series(base + [1e6] * 5), cfg)
    pd.testing.assert_series_equal(early, with_outlier.iloc[:200], check_names=False)


def _frame(series_id: str, times: pd.DatetimeIndex, values) -> pd.DataFrame:
    return pd.DataFrame({"timestamp": times, "value": values, "service": series_id, "series_id": series_id})


def test_leak_is_kept_when_the_overlapping_jump_comes_later():
    cfg = Config(leak_window_minutes=60, leak_rise_bytes=0.5e9, leak_min_points=10,
                 memory_jump_window_minutes=15, memory_jump_bytes=0.5e9)
    times = pd.date_range(T0, periods=400, freq="30s")
    values = np.full(400, 1e9)
    values[150:300] = np.linspace(1e9, 3e9, 150)  # slow climb: a leak
    values[300:] = 3e9
    values[290:296] = 5e9  # a fast jump near the end of the climb
    leaks = detect_leaks(_frame("db", times, values), cfg)
    assert leaks, "the leak confirmed before the jump must stay on the queue"
    assert all(leak.shown_at < times[290] for leak in leaks)


def test_dead_feed_is_reported_before_data_returns():
    cfg = Config(gap_minutes=10)
    times = pd.date_range(T0, periods=20, freq="30s")
    frame = pd.concat([
        _frame("web", times, np.ones(20)),
        _frame("db", pd.date_range(T0, periods=120, freq="30s"), np.ones(120)),
    ])
    (gap,) = detect_gaps(frame, cfg)  # web stopped; db runs on for an hour
    assert gap.service == "web" and gap.shown_at == times[-1] + pd.Timedelta(minutes=10)
    now = T0 + pd.Timedelta(hours=3)  # live: both feeds silent until now
    gaps = detect_gaps(frame, cfg, now=now)
    assert {s for g in gaps for s in g.service.split(",")} == {"web", "db"}


def test_alert_rides_on_a_jump_only_while_a_live_system_sees_it_open():
    jump = Spike("db", "db", T0, T0 + pd.Timedelta(minutes=10), confirm=T0 + pd.Timedelta(minutes=1),
                 grace=pd.Timedelta(minutes=2))
    assert not jump.open_at(T0 + pd.Timedelta(seconds=30))  # not shown yet
    assert jump.open_at(T0 + pd.Timedelta(minutes=5))
    assert jump.open_at(T0 + pd.Timedelta(minutes=11))  # last hot point 1 min ago: still open
    assert not jump.open_at(T0 + pd.Timedelta(minutes=13))
    cfg = Config(budget_k=5, alert_share=1.0)
    items = [QueueItem(jump.shown_at, "jump", "db", jump),
             QueueItem(T0 + pd.Timedelta(minutes=11), "alert", "db", _alert(0, 11)),
             QueueItem(T0 + pd.Timedelta(minutes=30), "alert", "db", _alert(1, 30))]
    shown = build_queue(items, cfg)
    assert [it.attached for it in shown] == [False, True, False]
