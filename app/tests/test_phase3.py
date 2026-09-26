"""Phase 3: slow leaks, cross-service grouping, carry-over budget, jump ranker."""
import pandas as pd
import pytest

from src.config import Config
from src.entities import Alert, Fault
from src.jump_ranker import JumpRanker, jump_features
from src.memory_spikes import Spike, detect_jumps, detect_leaks
from src.queue import build_queue, score_queue, spike_items

T0 = pd.Timestamp("2026-01-05", tz="UTC")


def _series(values, service="s1"):
    return pd.DataFrame({
        "timestamp": [T0 + pd.Timedelta(seconds=30 * i) for i in range(len(values))],
        "value": values,
        "service": service,
        "series_id": service,
    })


def _jump(minute, service, rise=1e9, day=0):
    start = T0 + pd.Timedelta(days=day, minutes=minute)
    return Spike(service, service, start, start + pd.Timedelta(minutes=5), rise,
                 confirm=start + pd.Timedelta(seconds=30), rise_at_confirm=rise)


def test_leak_rule_catches_a_ramp_the_jump_rule_misses():
    flat = [1e9] * 480                                  # 4 h flat
    ramp = [1e9 + 1e9 * i / 240 for i in range(240)]    # +1 GB over 2 h
    held = [2e9] * 60
    frame = _series(flat + ramp + held)
    cfg = Config(leak_window_minutes=180)
    assert detect_jumps(frame, cfg) == []
    (leak,) = detect_leaks(frame, cfg)
    assert leak.kind == "leak" and leak.start > T0 + pd.Timedelta(hours=4)


def test_leak_rule_skips_what_a_fast_jump_already_covers():
    frame = _series([1e9] * 480 + [2e9] * 40 + [1e9] * 100)
    cfg = Config(leak_window_minutes=180)
    assert len(detect_jumps(frame, cfg)) == 1
    assert detect_leaks(frame, cfg) == []


def test_leak_rule_can_be_turned_off():
    assert detect_leaks(_series([1e9] * 10), Config(leak_window_minutes=None)) == []


def test_items_on_other_services_within_the_window_share_a_slot():
    cfg = Config(budget_k=1, group_minutes=2)
    items = spike_items([_jump(10, "db"), _jump(11, "web"), _jump(30, "cache")], cfg)
    shown = build_queue(items, cfg)
    assert [(i.service, i.attached) for i in shown] == [("db", False), ("web", True)]
    faults = [Fault(f"f{s}", T0 + pd.Timedelta(minutes=m), T0 + pd.Timedelta(minutes=m + 5), s)
              for s, m in (("db", 10), ("web", 11), ("cache", 30))]
    row = score_queue("q", shown, faults, 1, cfg)
    assert row["faults_caught"] == 2 and row["items_per_day"] == 1


def test_unused_slots_carry_to_the_next_day_up_to_the_cap():
    cfg = Config(budget_k=2, carry_over_max=1)
    quiet = [_jump(10, "a", day=0)]                              # uses 1 of 2 -> carry 1
    busy = [_jump(10 + i * 20, f"s{i}", day=1) for i in range(5)]  # 2 + 1 carried
    shown = build_queue(spike_items(quiet + busy, cfg), cfg)
    assert sum(1 for i in shown if i.shown_at >= T0 + pd.Timedelta(days=1)) == 3
    no_carry = build_queue(spike_items(quiet + busy, Config(budget_k=2)), Config(budget_k=2))
    assert sum(1 for i in no_carry if i.shown_at >= T0 + pd.Timedelta(days=1)) == 2


def test_jump_ranker_learns_that_big_jumps_are_real():
    jumps, faults = [], []
    for i in range(40):
        big = i % 2 == 0
        j = _jump(i * 60, f"s{i % 3}", rise=1.1e9 if big else 0.55e9)
        j.level_at_confirm = 2e9
        jumps.append(j)
        if big:
            faults.append(Fault(f"f{i}", j.start, j.end, j.service))
    alerts = [Alert("a", T0, "s0", "log", "warning", "x")]
    ranker = JumpRanker(Config()).fit(jumps, alerts, faults)
    scores = ranker.score(jumps, alerts)
    assert scores[0] > scores[1]
    kept = ranker.keep(jumps, alerts)
    assert all(j.rise_at_confirm > 1e9 for j in kept) and len(kept) == 20


def test_jump_features_look_only_backwards():
    early, late = _jump(0, "a"), _jump(5, "b")
    frame = jump_features([early, late], [])
    assert frame.loc[0, "others_15m"] == 0 and frame.loc[1, "others_15m"] == 1


def test_ranker_needs_both_classes():
    with pytest.raises(ValueError):
        JumpRanker().fit([_jump(0, "a")], [], [])
