"""The committed GAIA sample loads, is real data, and reproduces its recorded counts."""
from __future__ import annotations

import json

import run_reproduce as rr
from src.gaia_loader import load_run_folder
from src.memory_spikes import load_metric_folder


def test_sample_loads_gzipped_run_tables_and_metrics():
    alerts, faults = load_run_folder(rr.SAMPLE / "run")
    metrics = load_metric_folder(rr.SAMPLE / "metric")
    # Requirement: a 10-day slice with the full 7-day training week plus test days.
    assert len(faults) > 100 and len(alerts) > 1000
    assert metrics["service"].nunique() >= 8
    span = metrics["timestamp"].max() - metrics["timestamp"].min()
    assert 9 <= span.days <= 10


def test_sample_counts_match_the_recorded_file_and_the_claim():
    got = rr.sample_counts()
    assert got == json.loads(rr.EXPECTED.read_text(encoding="utf-8"))
    for queues in got.values():
        for caught, total in queues.values():
            assert 0 <= caught <= total
    # Requirement (report section 3): the detector beats the rate rule when slots are scarce.
    assert got["K20"]["jumps_tuned"][0] > got["K20"]["delta_5m"][0]
