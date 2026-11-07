"""The live soak's matching and scoring logic, checked without Docker; plus the fleet's schedule."""
from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pandas as pd

import run_live_soak as soak

T0 = pd.Timestamp("2026-04-01 10:00", tz="UTC").timestamp()
FAULTS = [{"id": "f1", "service": "svc3", "start": T0, "end": T0 + 600},
          {"id": "f2", "service": "svc5", "start": T0 + 1200, "end": T0 + 1800}]


def _jump(service, start_s, end_s):
    return {"service": service, "start": pd.Timestamp(T0 + start_s, unit="s", tz="UTC").isoformat(),
            "end": pd.Timestamp(T0 + end_s, unit="s", tz="UTC").isoformat()}


def test_a_jump_matches_the_fault_on_its_own_service_only():
    assert soak.fault_for_jump(_jump("svc3", 5, 590), FAULTS) == "f1"
    assert soak.fault_for_jump(_jump("svc4", 5, 590), FAULTS) is None  # right time, wrong service
    assert soak.fault_for_jump(_jump("svc3", 5000, 5600), FAULTS) is None  # right service, long after


def test_a_jump_just_outside_the_window_still_matches_within_the_pad():
    assert soak.fault_for_jump(_jump("svc3", -50, 0), FAULTS) == "f1"      # 50 s early, pad is 60
    assert soak.fault_for_jump(_jump("svc3", -200, -100), FAULTS) is None


def test_an_alert_is_real_only_on_the_faulty_service_during_the_fault():
    inside = pd.Timestamp(T0 + 100, unit="s", tz="UTC").isoformat()
    after = pd.Timestamp(T0 + 600 + 60, unit="s", tz="UTC").isoformat()  # inside the 120 s tail
    late = pd.Timestamp(T0 + 600 + 600, unit="s", tz="UTC").isoformat()
    assert soak.alert_is_fault("svc3", inside, FAULTS)
    assert soak.alert_is_fault("svc3", after, FAULTS)
    assert not soak.alert_is_fault("svc3", late, FAULTS)
    assert not soak.alert_is_fault("svc1", inside, FAULTS)


def test_the_engineer_is_right_about_accuracy_of_the_time():
    rng = random.Random(7)
    marks = [soak.engineer_verdict(True, 0.9, rng) for _ in range(4000)]
    assert 0.88 < marks.count("real") / len(marks) < 0.92
    assert all(soak.engineer_verdict(True, 1.0, rng) == "real" for _ in range(50))
    assert all(soak.engineer_verdict(False, 1.0, rng) == "noise" for _ in range(50))


def test_summary_counts_only_settled_faults_and_measures_delay_by_hand():
    seen = {
        ("svc3", "a"): {"fault": "f1", "confirm": T0 + 90},   # caught 90 s after the fault began
        ("svc3", "b"): {"fault": "f1", "confirm": T0 + 300},  # a later duplicate must not change the delay
        ("svc9", "c"): {"fault": None, "confirm": T0 + 50},   # a false jump
    }
    now = T0 + 1200 + 600 + 30  # f2 ended 30 s ago: not settled yet (settle is 90 s)
    result = soak.summarize(FAULTS, seen, now)
    assert result["faults_injected"] == 2 and result["faults_judged"] == 1  # only f1 has settled
    assert result["faults_caught"] == 1 and result["detection_rate"] == 1.0
    assert result["median_delay_minutes"] == 1.5                           # 90 s
    assert result["shown_jumps"] == 3 and result["false_jumps"] == 1
    assert result["true_precision_jumps"] == round(1 - 1 / 3, 4)


def test_a_missed_fault_lowers_detection():
    result = soak.summarize(FAULTS, {}, T0 + 10_000)
    assert result["faults_judged"] == 2 and result["faults_caught"] == 0 and result["detection_rate"] == 0.0


# --- the fleet ---------------------------------------------------------------------------------


def _fleet():
    path = Path(__file__).parents[1] / "integration" / "live" / "fleet.py"
    spec = importlib.util.spec_from_file_location("fleet_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleet_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_a_fault_adds_one_gigabyte_only_while_it_lasts():
    fleet = _fleet()
    rng = random.Random(0)
    now = 1_000_000.0
    quiet = fleet.memory("svc3", now, rng)
    fleet.active["svc3"].append((now, now + 600, 1.0e9))
    during = fleet.memory("svc3", now + 100, rng)
    after = fleet.memory("svc3", now + 700, rng)
    assert 0.9e9 < during - quiet < 1.1e9  # the step, give or take the noise
    assert abs(after - quiet) < 0.2e9


def test_the_high_memory_rule_fires_only_on_services_that_start_high_enough():
    fleet = _fleet()
    fires = {s: fleet.BASELINE[s] + fleet.FAULT_BYTES > 3.2e9 for s in fleet.SERVICES}
    assert sum(fires.values()) == 3 and fires["svc6"] and not fires["svc1"]  # most faults raise no log alert


def test_faults_on_one_service_are_never_closer_than_the_jump_window(monkeypatch):
    """The jump rule compares with the median of the previous 15 minutes, so a second fault soon after
    the first has no rise to see. The first soak lost 6 of 19 faults that way (offline replay: 64 of 213)."""
    import argparse

    fleet = _fleet()

    class Stop(Exception):
        pass

    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds
        if clock[0] > 1.8e9 + 6 * 3600:
            raise Stop

    monkeypatch.setattr(fleet.time, "time", lambda: clock[0])
    monkeypatch.setattr(fleet.time, "sleep", sleep)
    for seed in range(5):
        clock[0] = 1.8e9
        for service in fleet.SERVICES:
            fleet.active[service].clear()
        fleet.truth.clear()
        try:
            fleet.schedule(argparse.Namespace(seed=seed, warmup=900, fault_every=300))
        except Stop:
            pass
        assert len(fleet.truth) > 20
        for service in fleet.SERVICES:
            mine = sorted((f for f in fleet.truth if f["service"] == service), key=lambda f: f["start"])
            for earlier, later in zip(mine, mine[1:]):
                assert later["start"] - earlier["end"] >= 15 * 60


def test_a_usual_storm_and_a_shifted_storm_differ_in_the_fields_the_drift_check_compares():
    rng = random.Random(3)
    end = pd.Timestamp("2026-04-01 12:00", tz="UTC")
    usual, usual_truth = soak.make_storm("usual", 200, end, 2.0, "u", rng)
    shifted, shifted_truth = soak.make_storm("shifted", 50, end, 2.0, "s", rng)
    assert {r["severity"] for r in usual} == {"warning", "info"} and {r["severity"] for r in shifted} == {"critical"}
    assert not {r["source"] for r in usual} & {r["source"] for r in shifted}
    assert all(shifted_truth.values()) and 0.15 < sum(usual_truth.values()) / len(usual_truth) < 0.45
    assert len({r["alert_id"] for r in usual + shifted}) == 250  # none is dropped as a duplicate
    assert all(end - pd.Timedelta(hours=2) <= pd.Timestamp(r["timestamp"]) <= end for r in usual)


def test_the_storm_alerts_trip_the_drift_report_when_the_mix_moves():
    from product.inputs import load_alerts_from_dicts
    from src.drift import drift_report

    rng = random.Random(4)
    end = pd.Timestamp("2026-04-01 12:00", tz="UTC")
    reference = load_alerts_from_dicts(soak.make_storm("usual", 120, end - pd.Timedelta(hours=20), 2.0, "r", rng)[0])
    same = load_alerts_from_dicts(soak.make_storm("usual", 40, end, 2.0, "a", rng)[0])
    moved = load_alerts_from_dicts(soak.make_storm("shifted", 40, end, 2.0, "b", rng)[0])
    assert drift_report(reference, same)["status"] == "ok"
    assert drift_report(reference, moved)["status"] == "warn"
