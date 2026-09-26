"""The one-day product sample must trip the built-in jump, gap and leak rules."""
from pathlib import Path

import pandas as pd

from src.config import Config
from src.memory_spikes import detect_gaps, detect_jumps, detect_leaks
from src.sample_data import generate_demo_day


def test_demo_day_has_a_jump_a_gap_and_a_leak(tmp_path: Path):
    generate_demo_day(tmp_path)
    frame = pd.read_csv(tmp_path / "metrics.csv")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["series_id"] = frame["service"]
    cfg = Config()
    jumps = detect_jumps(frame, cfg)
    gaps = detect_gaps(frame, cfg)
    leaks = detect_leaks(frame, cfg)
    assert len(jumps) >= 1 and jumps[0].rise_at_confirm >= cfg.memory_jump_bytes
    assert len(gaps) >= 1 and gaps[0].kind == "gap"
    assert len(leaks) >= 1 and leaks[0].kind == "leak"
    # The leak is a slow ramp: the 15-minute jump rule must not already cover it.
    assert not any(j.service == leaks[0].service for j in jumps)

    alerts = pd.read_csv(tmp_path / "alerts.csv")
    assert len(alerts) < 10
    jump = jumps[0]
    alert_ts = pd.to_datetime(alerts["timestamp"], utc=True)
    inside = (alerts["source"] == jump.service) & (alert_ts >= jump.start) & (alert_ts <= jump.end)
    assert int(inside.sum()) >= 1
    assert int((~inside).sum()) >= 1
