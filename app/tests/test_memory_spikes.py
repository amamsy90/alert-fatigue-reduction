"""A real memory jump becomes one episode. A single noisy point does not."""
import pandas as pd
import pytest

from src.config import Config
from src.entities import Alert, Fault
from src.features import build_feature_frame
from src.memory_spikes import (
    detect_gaps,
    detect_jumps,
    detect_spikes,
    faults_in_gaps,
    read_metric_csv,
    score_spikes,
)

T0 = pd.Timestamp("2026-01-05", tz="UTC")


def _metrics(values_by_minute: dict[int, float], service: str = "dbservice1") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [T0 + pd.Timedelta(minutes=minute) for minute in values_by_minute],
            "value": list(values_by_minute.values()),
            "service": service,
            "series_id": f"{service}_memory",
        }
    )


def test_spike_overlaps_the_injected_fault_and_ignores_one_point():
    # Week 1 is flat. Week 2 has a 10-minute jump and one stray high point.
    values = {minute: 100.0 for minute in range(0, 8 * 24 * 60, 10)}
    jump_at = 8 * 24 * 60
    for offset in range(0, 11):
        values[jump_at + offset] = 1000.0
    values[jump_at + 500] = 1000.0
    spikes = detect_spikes(_metrics(values), Config(memory_spike_k=3.0, memory_min_points=2))
    assert len(spikes) == 1
    fault = Fault(
        "f1",
        T0 + pd.Timedelta(minutes=jump_at),
        T0 + pd.Timedelta(minutes=jump_at + 10),
        "dbservice1",
    )
    scores = score_spikes(spikes, [fault])
    assert scores["faults_detected"] == 1
    assert scores["false_spikes"] == 0


def test_alert_inside_the_spike_is_marked():
    start = T0 + pd.Timedelta(days=8)
    spikes = detect_spikes(
        _metrics(
            {
                **{minute: 10.0 for minute in range(0, 7 * 24 * 60, 60)},
                **{7 * 24 * 60 + offset: 500.0 for offset in range(5)},
            }
        ),
        Config(),
    )
    inside = Alert("a1", spikes[0].confirm, "dbservice1", "cpu", "warning", "routine")
    # Before confirmation a live system does not know about the spike yet.
    early = Alert("a0", spikes[0].start, "dbservice1", "cpu", "warning", "routine")
    outside = Alert("a2", start - pd.Timedelta(hours=3), "dbservice1", "cpu", "warning", "routine")
    frame = build_feature_frame([outside, early, inside], spikes=spikes).set_index("alert_id")
    assert frame.loc["a1", "during_memory_spike"] == 1
    assert frame.loc["a0", "during_memory_spike"] == 0
    assert frame.loc["a2", "during_memory_spike"] == 0


def test_jump_rule_ignores_a_slow_drift_but_catches_a_sudden_rise():
    # Memory creeps up 2 GB over a day (a drift), then jumps 1 GB for 5 minutes.
    values = {minute: 1e9 + minute * (2e9 / 1440) for minute in range(0, 1440)}
    for offset in range(5):
        values[1440 + offset] = values[1439] + 1e9
    for minute in range(1445, 1500):
        values[minute] = values[1439]
    spikes = detect_jumps(_metrics(values), Config(memory_jump_bytes=0.5e9))
    assert len(spikes) == 1
    assert spikes[0].start == T0 + pd.Timedelta(minutes=1440)


def test_csv_roundtrip(tmp_path):
    path = tmp_path / "dbservice1_memory.csv"
    pd.DataFrame(
        {"timestamp": [1625097600000, 1625097660000], "value": [10, 12]}
    ).to_csv(path, index=False)
    frame = read_metric_csv(path, "dbservice1")
    assert list(frame["value"]) == [10.0, 12.0]
    assert frame["timestamp"].dt.tz is not None


def test_jump_knows_its_confirmation_point_and_rise_by_then():
    values = {minute: 1e9 for minute in range(0, 60)}
    values.update({60: 1.6e9, 61: 2.5e9, 62: 2.5e9})
    (jump,) = detect_jumps(_metrics(values), Config(memory_jump_bytes=0.5e9))
    assert jump.confirm == T0 + pd.Timedelta(minutes=61)
    assert jump.rise_at_confirm == pytest.approx(1.5e9)
    assert jump.peak_rise == pytest.approx(1.5e9)


def test_relative_mode_scales_with_the_metric():
    # A CPU-like series around 40 (%): +50% is a jump, +10% is not.
    values = {minute: 40.0 for minute in range(0, 60)}
    values.update({60: 60.0, 61: 60.0})
    values.update({minute: 40.0 for minute in range(62, 120)})
    values.update({120: 44.0, 121: 44.0})
    jumps = detect_jumps(_metrics(values), Config(memory_jump_mode="relative", memory_jump_ratio=0.3))
    assert [j.start for j in jumps] == [T0 + pd.Timedelta(minutes=60)]


def test_gaps_on_several_services_become_one_item():
    rows = []
    for service in ("dbservice1", "webservice1"):
        minutes = list(range(0, 30)) + list(range(90, 120))
        rows.append(_metrics({m: 1.0 for m in minutes}, service))
    (gap,) = detect_gaps(pd.concat(rows), Config(gap_minutes=10))
    assert set(gap.service.split(",")) == {"dbservice1", "webservice1"}
    assert gap.kind == "gap"
    assert gap.confirm == T0 + pd.Timedelta(minutes=39)
    fault = Fault("f1", T0 + pd.Timedelta(minutes=50), T0 + pd.Timedelta(minutes=60), "dbservice1")
    assert faults_in_gaps([gap], [fault]) == {"f1"}
