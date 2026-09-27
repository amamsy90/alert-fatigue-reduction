"""A per-series jump size is learned only where a series has enough incidents and it helps."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import Config
from src.entities import Fault
from src.generic import (
    DetectorSetting,
    detect,
    evaluate,
    setting_from_dict,
    split_points,
    tune_jump_size_by_series,
)


def _data(seed: int = 0):
    """Two series, 5-minute samples, 20 days. `quiet` has small faults on a flat
    line (a small size is needed); `noisy` has large faults on a jittery line
    (a small size floods it with false episodes)."""
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-01-01", periods=20 * 288, freq="5min", tz="UTC")
    quiet = 100 + rng.normal(0, 1.0, len(ts))
    noisy = 100 + rng.normal(0, 8.0, len(ts))
    faults = []
    for i, day in enumerate(range(1, 19)):
        start = ts[0] + pd.Timedelta(days=day, hours=6)
        sel = (ts >= start) & (ts < start + pd.Timedelta(minutes=30))
        quiet[sel] += 6.0
        noisy[sel] += 120.0
        for name in ("quiet", "noisy"):
            faults.append(Fault(f"{name}{i}", start, start + pd.Timedelta(minutes=30), name))
    frame = pd.concat([
        pd.DataFrame({"timestamp": ts, "value": quiet, "series_id": "quiet"}),
        pd.DataFrame({"timestamp": ts, "value": noisy, "series_id": "noisy"}),
    ], ignore_index=True)
    return frame, faults


def test_each_series_gets_the_size_it_needs():
    frame, faults = _data()
    cuts = split_points(frame, 0.5)
    train = [f for f in faults if f.start < cuts[f.component]]
    setting = DetectorSetting("jump", 12, 6.0, 2, "up")
    z_by = tune_jump_size_by_series(frame, train, cuts, setting, Config())
    assert set(z_by) <= {"quiet", "noisy"} and z_by
    # the learned sizes are never worse than the global one on the test part
    before = detect(frame, setting, cuts)
    after = detect(frame, DetectorSetting("jump", 12, 6.0, 2, "up", z_by_series=z_by), cuts)
    test = [f for f in faults if f.start >= cuts[f.component]]

    def caught(spikes):
        return sum(any(s.series_id == f.component and s.start <= f.end and s.end >= f.start for s in spikes) for f in test)

    assert caught(after) >= caught(before)


def test_too_few_incidents_means_no_per_series_size():
    frame, faults = _data()
    cuts = split_points(frame, 0.5)
    train = [f for f in faults if f.start < cuts[f.component]][:2]
    assert tune_jump_size_by_series(frame, train, cuts, DetectorSetting("jump", 12, 6.0, 2, "up"), Config()) == {}


def test_size_survives_the_saved_settings_round_trip(tmp_path):
    frame, faults = _data()
    frame.to_csv(tmp_path / "m.csv", index=False)
    pd.DataFrame([{"start": f.start, "end": f.end, "series": f.component} for f in faults]).to_csv(
        tmp_path / "i.csv", index=False)
    report = evaluate(tmp_path / "m.csv", tmp_path / "i.csv", budgets=(5,), train_fraction=0.5).to_dict()
    jump = report["settings"]["jump"]
    if jump.get("z_by_series"):
        back = setting_from_dict({"detector": {k: v for k, v in jump.items() if k != "label"}})
        assert back.z_by_series == jump["z_by_series"]
        assert "size per series" in back.label()
