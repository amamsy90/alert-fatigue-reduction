"""src/experiment.py produces every GAIA number; test it on a small synthetic run."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.config import Config
from src.entities import Alert, Fault
from src.experiment import (
    MISS_CAUSES,
    SYSTEMS,
    GaiaData,
    faults_with_metric_data,
    miss_causes,
    run_window,
    score_window,
)

T0 = pd.Timestamp("2026-03-01", tz="UTC")
SERVICES = ("webservice1", "dbservice1")


def _synthetic(days: int = 3, outage: tuple[pd.Timestamp, pd.Timestamp] | None = None) -> GaiaData:
    """Two services, 30 s memory samples, a 1 GB step for 10 minutes every 3 hours
    (alternating services), an error log line inside some faults, noise lines elsewhere."""
    times = pd.date_range(T0, T0 + pd.Timedelta(days=days), freq="30s", inclusive="left")
    faults, frames, alerts = [], [], []
    for s_idx, service in enumerate(SERVICES):
        rng = np.random.default_rng(7 + s_idx)  # one stream per service: a longer run keeps the same start
        values = 1e9 + rng.normal(0, 2e7, len(times))
        for k, start in enumerate(pd.date_range(T0 + pd.Timedelta(hours=1 + s_idx), times[-1], freq="3h")):
            if (k + s_idx) % 2:
                if k % 4 == 1:  # a false jump: memory moves, nothing is wrong
                    bump = (times >= start) & (times < start + pd.Timedelta(minutes=4))
                    values[bump] += 0.7e9
                continue
            end = start + pd.Timedelta(minutes=10)
            values[(times >= start) & (times < end)] += 1e9
            fid = f"{service}-{k}"
            faults.append(Fault(fid, start, end, service))
            if k % 3 == 0:
                alerts.append(Alert(f"a-{fid}", start + pd.Timedelta(minutes=2), service, "log", "error",
                                    "memory usage high", fid))
        keep = np.ones(len(times), dtype=bool)
        if outage and service == SERVICES[0]:
            keep &= ~((times >= outage[0]) & (times < outage[1]))
        frames.append(pd.DataFrame({"timestamp": times[keep], "value": values[keep],
                                    "service": service, "series_id": service}))
    for i, t in enumerate(pd.date_range(T0, times[-1], freq="47min")):
        alerts.append(Alert(f"n{i}", t, SERVICES[i % 2], "log", "info", f"heartbeat {i % 5}"))
    alerts.sort(key=lambda a: a.timestamp)
    metrics = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    return GaiaData(alerts, sorted(faults, key=lambda f: f.start), metrics, T0, times[-1])


@pytest.fixture(scope="module")
def data() -> GaiaData:
    return _synthetic()


CFG = Config(budget_k=5)
TRAIN_END = T0 + pd.Timedelta(days=1)
TEST_END = T0 + pd.Timedelta(days=3)


@pytest.fixture(scope="module")
def run(data):
    return run_window(data, TRAIN_END, TEST_END, CFG)


def test_every_system_is_scored_on_the_test_window_only(run):
    rows = score_window(run, CFG)
    assert [r["queue"] for r in rows] == list(SYSTEMS)
    assert all(f.start >= TRAIN_END for f in run.test_faults)
    assert all(r["faults"] == len(run.test_faults) for r in rows)
    assert run.n_days == 2


def test_the_jump_queue_catches_memory_steps_and_logs_do_not(run):
    rows = {r["queue"]: r for r in score_window(run, CFG)}
    assert rows["jumps_online"]["detection_rate"] >= 0.5 * rows["jumps_online"]["oracle_ceiling"]
    assert rows["proposed"]["detection_rate"] > rows["logs_severity"]["detection_rate"]
    assert rows["logs_severity"]["detection_rate"] < 0.5  # only some faults write a log line


def test_no_queue_takes_more_than_k_slots_a_day(run):
    for name in SYSTEMS:
        if name == "jumps_hindsight":
            continue  # the old top-K is per day as well, but has no attached items
        rows = score_window(run, CFG)
        row = next(r for r in rows if r["queue"] == name)
        limit = CFG.budget_k + (10 if "carryover" in name else 0)
        assert row["items_per_day"] <= limit


def test_later_data_does_not_change_the_window(data):
    longer = _synthetic(days=4)  # same first three days, one more after the test window
    a = score_window(run_window(data, TRAIN_END, TEST_END, CFG), CFG)
    b = score_window(run_window(longer, TRAIN_END, TEST_END, CFG), CFG)
    assert a == b


def test_misses_are_explained_and_add_up(run, data):
    rows = {r["queue"]: r for r in score_window(run, CFG)}
    causes = miss_causes(run, data, CFG)
    assert set(causes) == set(MISS_CAUSES)
    assert sum(causes.values()) == len(run.test_faults) - rows["proposed"]["faults_caught"]


def test_a_fault_during_an_outage_counts_as_no_metric_data():
    fault = Fault("x", T0 + pd.Timedelta(days=2, hours=1), T0 + pd.Timedelta(days=2, hours=1, minutes=10), SERVICES[0])
    outage = (fault.start - pd.Timedelta(minutes=5), fault.end + pd.Timedelta(minutes=5))
    blind = _synthetic(outage=outage)
    assert fault.fault_id not in faults_with_metric_data([fault], blind.metrics)
    assert fault.fault_id in faults_with_metric_data([fault], _synthetic().metrics)
    run = run_window(blind, TRAIN_END, TEST_END, CFG)
    missed_blind = [f for f in run.test_faults if f.component == SERVICES[0]
                    and outage[0] <= f.start and f.end <= outage[1]]
    assert missed_blind, "the outage must cover at least one injected fault"
    assert miss_causes(run, blind, CFG)["no_metric_data"] >= len(missed_blind)
