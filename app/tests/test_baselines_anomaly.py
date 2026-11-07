"""The statistical baselines fire on a real step, stay quiet on noise, and never look ahead."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.baselines_anomaly import detect_ewma, detect_iforest, detect_zscore
from src.config import Config

T0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")
GB = 1e9


def _metrics(values: np.ndarray, step_seconds: int = 30) -> pd.DataFrame:
    times = T0 + pd.to_timedelta(np.arange(len(values)) * step_seconds, unit="s")
    return pd.DataFrame({"timestamp": times, "value": values, "service": "svc", "series_id": "svc_mem"})


def _flat(n: int, seed: int = 0) -> np.ndarray:
    return 2 * GB + np.random.default_rng(seed).normal(0, 0.01 * GB, n)


def _with_step(n: int = 2000, at: int = 1500, rise: float = GB) -> np.ndarray:
    values = _flat(n)
    values[at : at + 20] += rise  # 10 minutes at 30 s
    return values


DETECTORS = {
    "zscore": lambda m, **kw: detect_zscore(m, **kw),
    "ewma": lambda m, **kw: detect_ewma(m, **kw),
    "iforest": lambda m, **kw: detect_iforest(m, fit_until=T0 + pd.Timedelta(minutes=500), **kw),
}


@pytest.mark.parametrize("name", DETECTORS)
def test_a_one_gigabyte_step_is_found_at_its_start(name):
    episodes = DETECTORS[name](_metrics(_with_step()), config=Config())
    step_start = T0 + pd.Timedelta(seconds=30 * 1500)
    assert any(abs((e.start - step_start).total_seconds()) <= 60 for e in episodes)
    assert all(e.kind == "rule" for e in episodes)


@pytest.mark.parametrize("name", DETECTORS)
def test_flat_noise_gives_no_episode(name):
    assert DETECTORS[name](_metrics(_flat(2000, seed=3)), config=Config()) == []


@pytest.mark.parametrize("name", DETECTORS)
def test_a_permanent_drop_is_not_a_fault(name):
    values = _flat(2000)
    values[1500:] -= GB  # level falls and stays down: only rises count
    assert DETECTORS[name](_metrics(values), config=Config()) == []


@pytest.mark.parametrize("name", DETECTORS)
def test_later_data_never_changes_an_earlier_decision(name):
    values = _with_step()
    cut = 1700
    early = DETECTORS[name](_metrics(values[:cut]), config=Config())
    longer = values.copy()
    longer[cut:] += 5 * GB  # a huge later change
    late = DETECTORS[name](_metrics(longer), config=Config())
    horizon = T0 + pd.Timedelta(seconds=30 * cut)
    assert [(e.start, e.confirm) for e in early if e.confirm < horizon] == [
        (e.start, e.confirm) for e in late if e.confirm < horizon
    ]
