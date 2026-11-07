"""Flags for the four queue experiments stay off unless set, and ignore later labels."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from experiments.model_ablation.model_eval import published_scoring_window
from src.config import Config
from src.enhancements import (
    FEATURES,
    TOPOLOGY_FEATURES,
    alertmanager_notifications,
    alertmanager_queue,
    choose_hybrid_m,
    company_calibration_enabled,
    compare_caught,
    delay_report,
    fanout_degree,
    fine_tune_ranker,
    first_catch_minutes,
    fit_topology_ranker,
    hybrid_queue,
    hybrid_queue_enabled,
    neighbour_services,
    note_catch_minutes,
    recalibrate_threshold,
    require_xgboost,
    slice_before,
    topology_features_enabled,
    topology_frame,
)
from src.entities import Alert, Fault
from src.memory_spikes import Spike
from src.queue import QueueItem, build_queue
from src.ranker import candidate_frame, fit_ranker, learned_ranker_enabled, select_by_score

T0 = pd.Timestamp("2026-03-01 00:00:00", tz="UTC")
APPS = {"acme": "online-boutique", "borealis": "otel-demo", "cassini": "sock-shop"}


def _jump(service: str, when: pd.Timestamp, rise: float, minutes: int = 10) -> QueueItem:
    spike = Spike(
        service,
        service,
        when,
        when + pd.Timedelta(minutes=minutes),
        confirm=when,
        rise_at_confirm=rise,
        level_at_confirm=1.0,
        kind="jump",
    )
    return QueueItem(when, "jump", service, spike)


def _alert(service: str, when: pd.Timestamp, fault_id: str | None = None) -> QueueItem:
    alert = Alert(f"a-{service}-{when}", when, service, "HighMemory", "warning", "up", fault_id)
    return QueueItem(when, "alert", service, alert)


def _fault(fault_id: str, when: pd.Timestamp, service: str) -> Fault:
    return Fault(fault_id, when, when + pd.Timedelta(minutes=20), service)


def _two_class_items():
    items = []
    faults = []
    for day in range(3):
        base = T0 + pd.Timedelta(days=day)
        items.append(_jump("api", base + pd.Timedelta(hours=1), 1.0))
        start = base + pd.Timedelta(hours=8)
        items.append(_jump("api", start, 40.0))
        faults.append(_fault(f"f{day}", start, "api"))
    return items, faults


def test_flags_default_off(monkeypatch):
    for name in ("RANKER_COMPANY_CALIBRATION", "HYBRID_QUEUE", "RANKER_TOPOLOGY_FEATURES", "LEARNED_RANKER"):
        monkeypatch.delenv(name, raising=False)
    assert company_calibration_enabled() is False
    assert hybrid_queue_enabled() is False
    assert topology_features_enabled() is False
    assert learned_ranker_enabled() is False


def test_default_feature_vector_stays_the_eleven_columns(monkeypatch):
    monkeypatch.setenv("RANKER_TOPOLOGY_FEATURES", "1")
    items, _faults = _two_class_items()
    frame = candidate_frame(items, None, Config())
    assert list(frame.columns) == list(FEATURES)
    assert len(FEATURES) == 11
    assert "neighbour_alerts_15m" not in frame.columns


def test_slice_before_drops_the_boundary_instant():
    boundary = T0 + pd.Timedelta(days=1)
    early = _jump("api", boundary - pd.Timedelta(minutes=1), 1.0)
    on_boundary = _jump("api", boundary, 1.0)
    fault_early = _fault("early", boundary - pd.Timedelta(hours=1), "api")
    fault_late = _fault("late", boundary, "api")
    items, faults = slice_before([early, on_boundary], [fault_early, fault_late], boundary)
    assert items == [early]
    assert [fault.fault_id for fault in faults] == ["early"]


def test_fine_tune_ignores_a_label_at_the_boundary():
    boundary = T0 + pd.Timedelta(days=1)
    early = _jump("api", T0 + pd.Timedelta(hours=8), 40.0)
    late = _jump("api", boundary, 1.0)
    faults = [_fault("early", early.shown_at, "api")]
    # The window has one class. The later item would add the other class.
    ranker, status = fine_tune_ranker([], [], [early, late], faults, Config(), boundary, enabled=True)
    assert status == "one-class"
    assert ranker is None


def test_fine_tune_is_deterministic_for_seed_42():
    items, faults = _two_class_items()
    boundary = T0 + pd.Timedelta(days=3)
    first, first_status = fine_tune_ranker(
        items[:2], faults[:1], items[2:], faults[1:], Config(), boundary, seed=42, enabled=True
    )
    second, second_status = fine_tune_ranker(
        items[:2], faults[:1], items[2:], faults[1:], Config(), boundary, seed=42, enabled=True
    )
    assert first_status == second_status == "trained"
    assert first is not None and second is not None
    assert np.array_equal(first.coef, second.coef)
    assert first.threshold == second.threshold


def test_recalibrate_off_returns_the_same_ranker():
    items, faults = _two_class_items()
    boundary = T0 + pd.Timedelta(days=2)
    source, status = fit_ranker(items, faults, Config(), None, train_end=boundary, backend="pointwise", seed=42)
    assert status == "trained" and source is not None
    adapted, why = recalibrate_threshold(source, items, faults, Config(), boundary, enabled=False)
    assert why == "off" and adapted is source


def test_recalibrate_matches_the_window_before_the_boundary():
    items, faults = _two_class_items()
    fit_end = T0 + pd.Timedelta(days=2)
    source, status = fit_ranker(items, faults, Config(), None, train_end=fit_end, backend="pointwise", seed=42)
    assert status == "trained" and source is not None
    boundary = T0 + pd.Timedelta(days=3)
    late = _jump("api", boundary, 1.0)
    with_late, late_status = recalibrate_threshold(source, items + [late], faults, Config(), boundary, enabled=True)
    without, early_status = recalibrate_threshold(source, items, faults, Config(), boundary, enabled=True)
    assert late_status == early_status == "retuned"
    assert with_late is not None and without is not None
    assert with_late.threshold == without.threshold
    assert np.array_equal(with_late.coef, source.coef)


class _PreferLater:
    def select(self, items, _scores, config):
        scores = [0.0 if item.service == "early" else 1.0 for item in items]
        return select_by_score(items, scores, config, 0.5)


def test_hybrid_flag_off_is_the_proposed_queue():
    day = T0 + pd.Timedelta(hours=1)
    items = [
        _jump("early", day, 1.0),
        _jump("mid", day + pd.Timedelta(hours=1), 5.0),
        _jump("late", day + pd.Timedelta(hours=2), 5.0),
    ]
    config = Config(budget_k=2)
    shown = hybrid_queue(items, _PreferLater(), config, 1, enabled=False)
    assert [item.service for item in shown] == [item.service for item in build_queue(items, config)]


def test_hybrid_fills_ranker_slots_before_proposed_slots():
    day = T0 + pd.Timedelta(hours=1)
    items = [
        _jump("early", day, 1.0),
        _jump("mid", day + pd.Timedelta(hours=1), 5.0),
        _jump("late", day + pd.Timedelta(hours=2), 5.0),
    ]
    shown = hybrid_queue(items, _PreferLater(), Config(budget_k=2), 1, enabled=True)
    # Ranker keeps the later high score. The open slot goes to the earliest proposed item.
    assert [item.service for item in shown if not item.attached] == ["mid", "early"]


def test_choose_m_is_stable_and_off_without_the_flag():
    items, faults = _two_class_items()
    boundary = T0 + pd.Timedelta(days=3)
    ranker, status = fit_ranker(items, faults, Config(budget_k=2), None, train_end=boundary, backend="pointwise", seed=42)
    assert status == "trained" and ranker is not None
    assert choose_hybrid_m(items, faults, ranker, Config(budget_k=2), [1, 2], enabled=False) is None
    first = choose_hybrid_m(items, faults, ranker, Config(budget_k=2), [2, 1], enabled=True)
    second = choose_hybrid_m(items, faults, ranker, Config(budget_k=2), [2, 1], enabled=True)
    assert first == second
    assert first in {1, 2}


def test_topology_flag_off_adds_no_columns_and_does_not_fit():
    items, faults = _two_class_items()
    frame = topology_frame(items, None, Config(), enabled=False)
    assert list(frame.columns) == list(FEATURES)
    ranker, status = fit_topology_ranker(items, faults, Config(), T0 + pd.Timedelta(days=3), enabled=False)
    assert ranker is None and status == "off"


def test_neighbour_count_uses_the_past_fifteen_minutes_and_the_same_company():
    stamp = T0 + pd.Timedelta(hours=2)
    host = _jump("acme/frontend", stamp, 2.0)
    recent = _alert("acme/checkoutservice", stamp - pd.Timedelta(minutes=10))
    future = _alert("acme/checkoutservice", stamp + pd.Timedelta(minutes=10))
    old = _alert("acme/checkoutservice", stamp - pd.Timedelta(minutes=20))
    other_company = _alert("cassini/checkoutservice", stamp - pd.Timedelta(minutes=5))
    frame = topology_frame(
        [host, recent, future, old, other_company], None, Config(), apps=APPS, enabled=True
    )
    assert list(frame.columns[-3:]) == list(TOPOLOGY_FEATURES)
    assert frame.loc[0, "neighbour_alerts_15m"] == 1
    assert fanout_degree("acme/frontend", APPS) == 7
    assert fanout_degree("gaia/unknown", APPS) == 0
    assert "acme/checkoutservice" in neighbour_services("acme/frontend", APPS)
    assert "acme/checkout" not in neighbour_services("acme/frontend", APPS)
    assert "borealis/checkoutservice" not in neighbour_services("borealis/frontend", APPS)


def test_same_service_jump_ignores_a_closed_episode():
    stamp = T0 + pd.Timedelta(hours=3)
    alert = _alert("acme/frontend", stamp)
    open_jump = _jump("acme/frontend", stamp - pd.Timedelta(minutes=5), 3.0, minutes=30)
    closed = _jump("acme/frontend", stamp - pd.Timedelta(hours=2), 3.0, minutes=10)
    frame = topology_frame([alert, open_jump, closed], None, Config(), apps=APPS, enabled=True)
    assert frame.loc[0, "same_service_jump"] == 1
    alone = topology_frame([alert, closed], None, Config(), apps=APPS, enabled=True)
    assert alone.loc[0, "same_service_jump"] == 0


def test_topology_fit_repeats_for_seed_42():
    items = [
        _jump("acme/frontend", T0 + pd.Timedelta(hours=1), 1.0),
        _jump("acme/frontend", T0 + pd.Timedelta(hours=8), 40.0),
        _jump("acme/checkoutservice", T0 + pd.Timedelta(days=1, hours=1), 1.0),
        _jump("acme/checkoutservice", T0 + pd.Timedelta(days=1, hours=8), 40.0),
    ]
    faults = [
        _fault("a", T0 + pd.Timedelta(hours=8), "acme/frontend"),
        _fault("b", T0 + pd.Timedelta(days=1, hours=8), "acme/checkoutservice"),
    ]
    end = T0 + pd.Timedelta(days=2)
    first, first_status = fit_topology_ranker(items, faults, Config(), end, seed=42, apps=APPS, enabled=True)
    second, second_status = fit_topology_ranker(items, faults, Config(), end, seed=42, apps=APPS, enabled=True)
    assert first_status == second_status == "trained"
    assert first is not None and second is not None
    assert np.array_equal(first.coef, second.coef)
    assert first.threshold == second.threshold


def test_delay_skips_gaps_and_the_paired_median_ci_is_exact():
    start = T0 + pd.Timedelta(hours=4)
    fault = _fault("f", start, "api")
    gap_spike = Spike("g", "api", start, start + pd.Timedelta(minutes=5), confirm=start, kind="gap")
    gap = QueueItem(start + pd.Timedelta(minutes=1), "gap", "api", gap_spike)
    jump = _jump("api", start + pd.Timedelta(minutes=6), 4.0)
    delays = first_catch_minutes([gap, jump], [fault])
    assert delays == {"f": 6.0}
    report = delay_report({"a": 10.0, "b": 30.0}, {"a": 20.0, "b": 40.0}, n_draw=20, seed=42)
    assert report["median_min"] == 20.0
    assert report["median_diff"] == -10.0
    assert report["low"] == -10.0 and report["high"] == -10.0
    assert report["n_both"] == 2


def test_keep_requires_the_interval_lower_bound_above_zero():
    faults = [_fault("a", T0, "api"), _fault("b", T0, "api")]
    kept = compare_caught({"a", "b"}, set(), faults, n_draw=20, seed=42)
    assert kept["decision"] == "KEEP"
    assert kept["low"] > 0
    dropped = compare_caught({"a"}, {"a"}, faults, n_draw=20, seed=42)
    assert dropped["decision"] == "DROP"
    assert dropped["low"] <= 0
    empty = compare_caught(set(), set(), [], n_draw=20, seed=42)
    assert empty["decision"] is None


def test_published_gaia_window_ignores_metrics_and_faults_outside_the_alerts():
    """The published split starts at the first alert and stops one second after the last.

    A metric point before that alert, or a fault after the last alert, must not
    add a test fault. The home-lab span would include both.
    """
    first = T0 + pd.Timedelta(days=10)
    last = T0 + pd.Timedelta(days=20)
    alerts = [
        Alert("a1", first, "api", "error", "error", "up"),
        Alert("a2", last, "api", "error", "error", "up"),
    ]
    metrics = pd.DataFrame(
        {
            "timestamp": [T0, last + pd.Timedelta(days=5)],
            "value": [1.0, 2.0],
            "service": ["api", "api"],
            "series_id": ["api", "api"],
        }
    )
    early = Fault("early", T0 + pd.Timedelta(days=8), T0 + pd.Timedelta(days=8, hours=1), "api")
    held = Fault("held", T0 + pd.Timedelta(days=18), T0 + pd.Timedelta(days=18, hours=1), "api")
    late = Fault("late", T0 + pd.Timedelta(days=25), T0 + pd.Timedelta(days=25, hours=1), "api")
    origin, train_end, stop, frame, test_faults = published_scoring_window(
        metrics, alerts, [early, held, late], Config(split_day=7)
    )
    assert origin == first
    assert train_end == first + pd.Timedelta(days=7)
    assert stop == last + pd.Timedelta(seconds=1)
    assert [fault.fault_id for fault in test_faults] == ["held"]
    assert frame["timestamp"].max() < stop
    assert early.start < train_end
    assert late.start >= stop


def test_xgboost_backend_fails_when_the_library_is_missing():
    from src.ranker import xgboost_importable

    if xgboost_importable():
        require_xgboost("xgboost")
        return
    with pytest.raises(SystemExit, match="xgboost cannot be imported"):
        require_xgboost("xgboost")
    require_xgboost("pointwise")


def test_alertmanager_drops_the_null_route_and_respects_the_budget_order():
    early = Alert("e", T0 + pd.Timedelta(hours=1), "api", "HighMemory", "warning", "up", "f1")
    late = Alert("l", T0 + pd.Timedelta(hours=2), "api", "DiskFull", "critical", "up", "f2")
    watchdog = Alert("w", T0 + pd.Timedelta(minutes=5), "api", "Watchdog", "critical", "up", "f3")
    notes = alertmanager_notifications([late, watchdog, early], Config())
    assert all("Watchdog" not in note["group"] for note in notes)
    config = Config(budget_k=1)
    severity = alertmanager_queue(notes, config, "severity")
    first = alertmanager_queue(notes, config, "first")
    assert severity[0]["alerts"][0].alert_id == "l"
    assert first[0]["alerts"][0].alert_id == "e"
    faults = [_fault("f1", early.timestamp, "api"), _fault("f2", late.timestamp, "api")]
    assert set(note_catch_minutes(severity, faults)) == {"f2"}
