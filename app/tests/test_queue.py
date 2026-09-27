"""The causal queue: first come, K a day, alerts capped, alerts ride on jumps."""
import pandas as pd

from src.baselines_metric import detect_delta, detect_static
from src.config import Config
from src.entities import Alert, Fault
from src.memory_spikes import Spike
from src.queue import (
    QueueItem,
    build_queue,
    hindsight_top_k,
    oracle_ceiling,
    score_queue,
    spike_items,
    tune_confirm_threshold,
)

T0 = pd.Timestamp("2026-01-05", tz="UTC")


def _jump(minute, service="s1", rise=1e9, peak=None, length=5):
    start = T0 + pd.Timedelta(minutes=minute)
    return Spike(
        service, service, start, start + pd.Timedelta(minutes=length), peak or rise,
        confirm=start + pd.Timedelta(seconds=30), rise_at_confirm=rise,
    )


def _alert(minute, service="s1", fault_id=None, aid=None):
    return Alert(aid or f"a{minute}", T0 + pd.Timedelta(minutes=minute), service, "log", "warning", "x", fault_id)


def test_first_come_first_served_not_biggest_first():
    early_small = _jump(10, rise=0.6e9, peak=0.6e9)
    late_big = _jump(20, rise=3e9, peak=3e9)
    cfg = Config(budget_k=1)
    online = build_queue(spike_items([late_big, early_small], cfg), cfg)
    assert [item.payload for item in online] == [early_small]
    # The old queue waits for the day to end and keeps the biggest peak.
    assert [item.payload for item in hindsight_top_k([early_small, late_big], cfg)] == [late_big]


def test_confirm_threshold_filters_small_jumps():
    cfg = Config(confirm_min_rise=0.8e9)
    items = spike_items([_jump(1, rise=0.6e9), _jump(9, rise=0.9e9)], cfg)
    assert len(items) == 1


def test_alerts_cannot_crowd_out_the_budget():
    cfg = Config(budget_k=4, alert_share=0.25)
    alerts = [QueueItem(a.timestamp, "alert", a.source, a) for a in (_alert(m, "s9") for m in range(10))]
    jumps = spike_items([_jump(100, "s2"), _jump(200, "s3")], cfg)
    shown = build_queue(alerts + jumps, cfg)
    assert sum(item.kind == "alert" for item in shown) == 1
    assert sum(item.kind == "jump" for item in shown) == 2


def test_a_jump_at_the_same_instant_as_an_alert_keeps_the_slot():
    """An alert timestamped on the confirmation point must not take the only slot."""
    cfg = Config(budget_k=1, alert_share=1.0)
    jump = _jump(10)
    alert = Alert("a-tie", jump.shown_at, "s1", "log", "error", "during jump", "f1")
    shown = build_queue(
        spike_items([jump], cfg) + [QueueItem(alert.timestamp, "alert", "s1", alert)],
        cfg,
    )
    assert [item.kind for item in shown] == ["jump", "alert"]
    assert shown[1].attached
    score = score_queue("q", shown, [], 1, cfg)
    assert score["items_per_day"] == 1


def test_alert_inside_a_shown_jump_rides_on_it():
    cfg = Config(budget_k=1, alert_share=1.0)
    jump = _jump(10)
    inside = _alert(12)
    shown = build_queue(spike_items([jump], cfg) + [QueueItem(inside.timestamp, "alert", "s1", inside)], cfg)
    assert len(shown) == 2 and shown[1].attached
    score = score_queue("q", shown, [], 1, cfg)
    assert score["items_per_day"] == 1 and score["attached_alerts"] == 1


def test_score_counts_catches_false_items_and_blind_faults():
    cfg = Config()
    fault = Fault("f1", T0 + pd.Timedelta(minutes=9), T0 + pd.Timedelta(minutes=19), "s1")
    blind = Fault("f2", T0 + pd.Timedelta(minutes=300), T0 + pd.Timedelta(minutes=310), "s2")
    gap = Spike("gap", "s2", T0 + pd.Timedelta(minutes=290), T0 + pd.Timedelta(minutes=400),
                confirm=T0 + pd.Timedelta(minutes=300), kind="gap")
    shown = build_queue(spike_items([_jump(10), _jump(500, "s3"), gap], cfg), cfg)
    row = score_queue("q", shown, [fault, blind], 1, cfg)
    assert row["faults_caught"] == 1
    assert row["false_items_per_day"] == 1
    assert row["blind_faults_flagged"] == 1
    assert row["confirm_minutes_median"] == 1.5


def test_oracle_ceiling_is_the_budget_share_of_faults():
    faults = [Fault(f"f{i}", T0 + pd.Timedelta(minutes=i), T0 + pd.Timedelta(minutes=i + 1), "s") for i in range(30)]
    assert oracle_ceiling(faults, Config(budget_k=20)) == 20 / 30


def test_threshold_is_learned_from_training_faults():
    fault = Fault("f1", T0 + pd.Timedelta(minutes=9), T0 + pd.Timedelta(minutes=20), "s1")
    real = _jump(10, rise=1.0e9)
    noise = [_jump(100 + 20 * i, rise=0.55e9) for i in range(5)]
    thr = tune_confirm_threshold([real, *noise], [fault], Config())
    assert 0.55e9 < thr <= 1.0e9


def _series(values, service="s1"):
    return pd.DataFrame({
        "timestamp": [T0 + pd.Timedelta(minutes=m) for m in values],
        "value": list(values.values()),
        "service": service,
        "series_id": service,
    })


def test_prometheus_style_baselines_fire_on_a_step():
    values = {m: 1e9 for m in range(0, 120)}
    values.update({m: 2e9 for m in range(120, 130)})
    frame = _series(values)
    delta = detect_delta(frame, Config(), minutes=5, rise=0.5e9)
    static = detect_static(frame, Config(), fit_until=T0 + pd.Timedelta(minutes=100))
    assert len(delta) == 1 and delta[0].start == T0 + pd.Timedelta(minutes=120)
    assert len(static) == 1 and static[0].kind == "rule"


def test_day_offset_moves_every_daily_budget_together():
    from src.baseline import baseline_decisions
    from src.config import day_of
    from src.entities import Action

    # 23:30 UTC is already the next day in China (UTC+8).
    late = Alert("a1", pd.Timestamp("2026-01-05 23:30", tz="UTC"), "s1", "error", "error", "x1")
    early = Alert("a2", pd.Timestamp("2026-01-06 00:30", tz="UTC"), "s1", "error", "error", "x2")
    utc, china = Config(budget_k=1), Config(budget_k=1, day_offset_hours=8)
    assert day_of(late.timestamp, utc) != day_of(early.timestamp, utc)
    assert day_of(late.timestamp, china) == day_of(early.timestamp, china)
    both_shown = [d.action for d in baseline_decisions([late, early], utc)]
    one_shown = [d.action for d in baseline_decisions([late, early], china)]
    assert both_shown == [Action.ESCALATE, Action.ESCALATE]
    assert one_shown == [Action.ESCALATE, Action.SUPPRESS]
