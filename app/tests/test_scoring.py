"""The shared definition of a catch and the shared threshold search."""
from __future__ import annotations

import pandas as pd

from src.config import Config
from src.entities import Fault
from src.memory_spikes import Spike
from src.scoring import hit_sets, lowest_cost_threshold, overlaps, spike_hits, threshold_cost

T0 = pd.Timestamp("2026-01-01", tz="UTC")


def _t(minutes: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(minutes=minutes)


def _spike(service: str, a: float, b: float) -> Spike:
    return Spike(service, service, _t(a), _t(b))


def test_touching_windows_overlap_and_disjoint_ones_do_not():
    fault = Fault("f", _t(10), _t(20), "db")
    assert overlaps(_spike("db", 20, 25), fault)
    assert overlaps(_spike("db", 5, 10), fault)
    assert overlaps(_spike("db", 12, 14), fault)
    assert not overlaps(_spike("db", 21, 30), fault)


def test_a_catch_needs_the_same_service():
    faults = [Fault("f1", _t(10), _t(20), "db"), Fault("f2", _t(10), _t(20), "web")]
    by = {"db": [faults[0]], "web": [faults[1]]}
    assert [f.fault_id for f in spike_hits(_spike("db", 15, 16), by)] == ["f1"]
    assert hit_sets([_spike("db", 15, 16), _spike("cache", 15, 16)], faults) == [{"f1"}, set()]


def test_threshold_cost_counts_distinct_faults_and_false_items():
    cfg = Config()  # a miss costs 10, a false item 1
    values = [5.0, 4.0, 1.0]
    hits = [{"a"}, {"a"}, set()]
    cost, caught, false = threshold_cost(values, hits, n_faults=2, threshold=0.0, config=cfg)
    assert (caught, false) == (1, 1)
    assert cost == cfg.miss_cost * 1 + cfg.false_alarm_cost * 1


def test_the_lowest_cost_threshold_drops_the_noise_below_it():
    cfg = Config()
    values = [0.2, 0.3, 0.25, 5.0, 6.0]
    hits = [set(), set(), set(), {"a"}, {"b"}]
    thr = lowest_cost_threshold(values, hits, n_faults=2, config=cfg)
    assert thr is not None and 0.3 < thr <= 5.0
    assert lowest_cost_threshold([], [], n_faults=0, config=cfg) is None
