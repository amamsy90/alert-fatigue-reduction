"""Standard anomaly detectors as extra baselines for the jump rule.

`baselines_metric.py` holds the rules a team already runs in Prometheus. These
are the textbook statistical detectors a reviewer would ask about next:

* zscore:  value more than `z` standard deviations above the mean of the previous hour
* ewma:    value more than `k` exponentially weighted standard deviations above the
           exponentially weighted mean of the past
* iforest: an isolation forest fitted per series on the training window, on the rise
           over 5 minutes ago and over the previous 15-minute median

All three use only points before the one they judge (the rolling and exponential
windows are shifted by one point; the forest is fitted before `detect_from`), and
all return `Spike` episodes with kind="rule", so the same causal queue and scorer
apply to every system. Only upward moves count: a fault here is extra memory.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from .config import Config
from .memory_spikes import Spike, _episodes

MIN_POINTS = 20  # history a series needs before a detector judges it


def _series(part: pd.DataFrame) -> pd.Series:
    return part.sort_values("timestamp").set_index("timestamp")["value"].astype(float)


def _window(
    hot: pd.Series, detect_from: pd.Timestamp | None, detect_until: pd.Timestamp | None
) -> pd.Series:
    if detect_from is not None:
        hot = hot[hot.index >= detect_from]
    if detect_until is not None:
        hot = hot[hot.index < detect_until]
    return hot


def _rule_episodes(hot: pd.Series, part: pd.DataFrame, series_id: str, config: Config) -> list[Spike]:
    episodes = _episodes(hot, series_id, str(part["service"].iloc[0]), config)
    for episode in episodes:
        episode.kind = "rule"
    return episodes


def detect_zscore(
    metrics: pd.DataFrame,
    config: Config | None = None,
    *,
    window_minutes: int = 60,
    z: float = 3.0,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
) -> list[Spike]:
    config = config or Config()
    out: list[Spike] = []
    for series_id, part in metrics.groupby("series_id"):
        series = _series(part)
        past = series.shift(1).rolling(f"{window_minutes}min", min_periods=MIN_POINTS)
        mean, std = past.mean(), past.std()
        score = (series - mean) / std.where(std > 0)
        hot = (series - mean)[(score > z) & (series > mean)]
        out += _rule_episodes(_window(hot, detect_from, detect_until), part, str(series_id), config)
    return out


def detect_ewma(
    metrics: pd.DataFrame,
    config: Config | None = None,
    *,
    span_points: int = 120,
    k: float = 3.0,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
) -> list[Spike]:
    config = config or Config()
    out: list[Spike] = []
    for series_id, part in metrics.groupby("series_id"):
        series = _series(part)
        ew = series.ewm(span=span_points, adjust=False, min_periods=MIN_POINTS)
        mean, std = ew.mean().shift(1), ew.std().shift(1)
        hot = (series - mean)[((series - mean) > k * std) & (std > 0)]
        out += _rule_episodes(_window(hot, detect_from, detect_until), part, str(series_id), config)
    return out


def _forest_features(series: pd.Series, step: pd.Timedelta) -> pd.DataFrame:
    five = max(1, int(round(pd.Timedelta(minutes=5) / step)))
    fifteen = max(2, int(round(pd.Timedelta(minutes=15) / step)))
    median = series.shift(1).rolling(fifteen, min_periods=fifteen // 2).median()
    return pd.DataFrame({"over_5min": series - series.shift(five), "over_median": series - median})


def detect_iforest(
    metrics: pd.DataFrame,
    config: Config | None = None,
    *,
    fit_until: pd.Timestamp,
    contamination: float = 0.001,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
    seed: int = 0,
) -> list[Spike]:
    config = config or Config()
    out: list[Spike] = []
    for series_id, part in metrics.groupby("series_id"):
        series = _series(part)
        if len(series) < 2 * MIN_POINTS:
            continue
        step = pd.Timedelta(np.median(np.diff(series.index.to_numpy()).astype("timedelta64[ns]").astype("int64")), unit="ns")
        feats = _forest_features(series, step).dropna()
        train = feats[feats.index < fit_until]
        if len(train) < MIN_POINTS:
            continue
        forest = IsolationForest(n_estimators=100, contamination=contamination, random_state=seed)
        forest.fit(train.to_numpy())
        test = _window(feats["over_median"], detect_from, detect_until)
        if test.empty:
            continue
        flagged = forest.predict(feats.loc[test.index].to_numpy()) == -1
        hot = test[flagged & (test.to_numpy() > 0)]
        out += _rule_episodes(hot, part, str(series_id), config)
    return out
