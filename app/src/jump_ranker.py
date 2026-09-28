"""Learned jump ranker (Phase 3): score each metric item at its confirmation point.

The alert classifier adds little on GAIA because most faults write no log. This
model instead scores the metric items themselves, using only what is known at
confirmation time:

  rise_at_confirm, rise relative to the recent level, service,
  other services' items confirmed in the previous 15 minutes,
  items on the same service in the previous 24 hours (a flapping series),
  log lines (and warnings/errors) on the service in the previous 5 minutes,
  hour of day.

Labels: the item overlaps a known fault (training window) or a Real mark.
The show/hide threshold minimises 10 x missed faults + false items on the
training window, like `tune_confirm_threshold`.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .config import Config
from .entities import Alert, Fault
from .memory_spikes import Spike
from .scoring import hit_sets, lowest_cost_threshold

NUMERIC = [
    "log_rise",
    "rise_ratio",
    "others_15m",
    "same_24h",
    "logs_5m",
    "warn_5m",
    "hour_sin",
    "hour_cos",
]


def jump_features(spikes: list[Spike], alerts: list[Alert]) -> pd.DataFrame:
    """One row per spike, same order. Every feature looks only backwards from `shown_at`."""
    order = sorted(spikes, key=lambda s: s.shown_at)
    times = [s.shown_at.value for s in order]
    services = [s.service for s in order]
    by_service_times: dict[str, list[int]] = defaultdict(list)
    for s in order:
        by_service_times[s.service].append(s.shown_at.value)
    alert_times: dict[str, list[int]] = defaultdict(list)
    warn_times: dict[str, list[int]] = defaultdict(list)
    for a in sorted(alerts, key=lambda a: a.timestamp):
        alert_times[a.source].append(a.timestamp.value)
        if a.severity in ("warning", "error", "critical", "high"):
            warn_times[a.source].append(a.timestamp.value)
    m15, h24, m5 = (int(pd.Timedelta(x).value) for x in ("15min", "24h", "5min"))
    rows = []
    for s in spikes:
        t = s.shown_at.value
        lo = bisect_left(times, t - m15)
        hi = bisect_left(times, t)
        others = sum(1 for i in range(lo, hi) if services[i] != s.service)
        same = by_service_times[s.service]
        same_24h = bisect_left(same, t) - bisect_left(same, t - h24)
        warns = warn_times.get(s.service, [])
        logs = alert_times.get(s.service, [])
        hour = s.shown_at.hour + s.shown_at.minute / 60
        rise = max(float(s.rise_at_confirm), 1.0)
        rows.append(
            {
                "service": s.service,
                "log_rise": math.log10(rise),
                "rise_ratio": rise / s.level_at_confirm if s.level_at_confirm > 0 else 0.0,
                "others_15m": others,
                "same_24h": same_24h,
                "logs_5m": bisect_right(logs, t) - bisect_left(logs, t - m5),
                "warn_5m": bisect_right(warns, t) - bisect_left(warns, t - m5),
                "hour_sin": math.sin(2 * math.pi * hour / 24),
                "hour_cos": math.cos(2 * math.pi * hour / 24),
            }
        )
    return pd.DataFrame(rows, columns=["service", *NUMERIC])


def _labels(spikes: list[Spike], faults: list[Fault]) -> tuple[np.ndarray, list[set[str]]]:
    hits = hit_sets(spikes, faults)
    return np.array([bool(h) for h in hits]), hits


class JumpRanker:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.pipe: Pipeline | None = None
        self.threshold: float = 0.0

    def fit(self, spikes: list[Spike], alerts: list[Alert], faults: list[Fault]) -> JumpRanker:
        spikes = [s for s in spikes if s.kind in ("jump", "leak")]
        y, hits = _labels(spikes, faults)
        if len(set(y.tolist())) < 2:
            raise ValueError("Jump ranker needs both real and false jumps to learn from")
        frame = jump_features(spikes, alerts)
        weights = np.where(y, self.config.miss_cost, self.config.false_alarm_cost)
        self.pipe = Pipeline(
            [
                (
                    "features",
                    ColumnTransformer(
                        [
                            ("num", StandardScaler(), NUMERIC),
                            ("svc", OneHotEncoder(handle_unknown="ignore"), ["service"]),
                        ]
                    ),
                ),
                ("clf", LogisticRegression(max_iter=1000, C=1.0, random_state=self.config.random_seed)),
            ]
        )
        self.pipe.fit(frame, y, clf__sample_weight=weights)
        scores = self.pipe.predict_proba(frame)[:, 1]
        self.threshold = _tune(scores, hits, faults, self.config)
        return self

    def score(self, spikes: list[Spike], alerts: list[Alert]) -> np.ndarray:
        if self.pipe is None:
            raise RuntimeError("Jump ranker has not been fit")
        if not spikes:
            return np.array([])
        return self.pipe.predict_proba(jump_features(spikes, alerts))[:, 1]

    def keep(self, spikes: list[Spike], alerts: list[Alert]) -> list[Spike]:
        """Metric items the ranker would show (gaps always pass)."""
        ranked = [s for s in spikes if s.kind in ("jump", "leak")]
        scores = dict(zip(map(id, ranked), self.score(ranked, alerts)))
        return [s for s in spikes if s.kind not in ("jump", "leak") or scores[id(s)] >= self.threshold]


def _tune(scores: np.ndarray, hits: list[set[str]], faults: list[Fault], config: Config) -> float:
    thr = lowest_cost_threshold(list(scores), hits, len(faults), config)
    return 0.0 if thr is None else thr
