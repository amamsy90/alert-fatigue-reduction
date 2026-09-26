"""Metric baselines a real team would already have (fair comparison for the jump rule).

Both are plain alerting rules, written the way a Prometheus rule would be:

* static:  value > training-window quantile (e.g. p99), per series
* delta:   value - value 5 minutes ago > X   (`delta(mem[5m]) > X`)

They return `Spike` episodes (kind="rule") so the same causal queue and scorer
apply to every system.
"""
from __future__ import annotations

import pandas as pd

from .config import Config
from .memory_spikes import Spike, _episodes


def detect_static(
    metrics: pd.DataFrame,
    config: Config | None = None,
    *,
    fit_until: pd.Timestamp,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
    quantile: float = 0.99,
) -> list[Spike]:
    config = config or Config()
    out: list[Spike] = []
    for series_id, part in metrics.groupby("series_id"):
        series = part.sort_values("timestamp").set_index("timestamp")["value"]
        train = series[series.index < fit_until]
        if train.empty:
            continue
        cutoff = float(train.quantile(quantile))
        hot = series[series > cutoff] - cutoff
        if detect_from is not None:
            hot = hot[hot.index >= detect_from]
        if detect_until is not None:
            hot = hot[hot.index < detect_until]
        for ep in _episodes(hot, str(series_id), str(part["service"].iloc[0]), config):
            ep.kind = "rule"
            out.append(ep)
    return out


def detect_delta(
    metrics: pd.DataFrame,
    config: Config | None = None,
    *,
    minutes: int = 5,
    rise: float | None = None,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
) -> list[Spike]:
    config = config or Config()
    rise = config.memory_jump_bytes if rise is None else rise
    tolerance = pd.Timedelta(minutes=1)
    out: list[Spike] = []
    for series_id, part in metrics.groupby("series_id"):
        frame = part.sort_values("timestamp")[["timestamp", "value"]].reset_index(drop=True)
        frame["timestamp"] = frame["timestamp"].astype("datetime64[ns, UTC]")
        past = frame.rename(columns={"value": "past"}).copy()
        past["timestamp"] = (past["timestamp"] + pd.Timedelta(minutes=minutes)).astype(
            "datetime64[ns, UTC]"
        )
        joined = pd.merge_asof(frame, past, on="timestamp", tolerance=tolerance, direction="nearest")
        diff = (joined["value"] - joined["past"]).set_axis(joined["timestamp"])
        hot = diff[diff > rise]
        if detect_from is not None:
            hot = hot[hot.index >= detect_from]
        if detect_until is not None:
            hot = hot[hot.index < detect_until]
        for ep in _episodes(hot, str(series_id), str(part["service"].iloc[0]), config):
            ep.kind = "rule"
            out.append(ep)
    return out
