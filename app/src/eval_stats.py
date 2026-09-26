"""Numbers a marker can defend: intervals, a test vs baseline, and why faults are missed."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .entities import Alert, Fault
from .memory_spikes import Spike


@dataclass(frozen=True)
class MeanInterval:
    mean: float
    low: float
    high: float
    n: int


@dataclass(frozen=True)
class SignTest:
    wins: int
    n: int
    p_value: float


def bootstrap_mean_ci(
    values: list[float],
    n_draw: int = 10_000,
    seed: int = 42,
) -> MeanInterval:
    """95% interval for the mean of a small list (walk-forward fold rates)."""
    if not values:
        raise ValueError("Need at least one value")
    arr = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    draws = rng.choice(arr, size=(n_draw, len(arr)), replace=True).mean(axis=1)
    return MeanInterval(
        mean=float(arr.mean()),
        low=float(np.percentile(draws, 2.5)),
        high=float(np.percentile(draws, 97.5)),
        n=len(arr),
    )


def sign_test_greater(left: list[float], right: list[float]) -> SignTest:
    """One-sided sign test: P(X >= wins) if each non-tie fold is a fair coin.

    Ties are dropped. 8/8 wins is 1/256. A 5/8 week must not keep that tiny p.
    """
    if len(left) != len(right):
        raise ValueError("Fold lists must be the same length")
    wins = ties = 0
    for a, b in zip(left, right):
        if a > b:
            wins += 1
        elif a == b:
            ties += 1
    n = len(left) - ties
    if n == 0:
        p = 1.0
    else:
        p = sum(math.comb(n, k) for k in range(wins, n + 1)) / float(2**n)
    return SignTest(wins=wins, n=n, p_value=p)


def exclusive_window_days(start: pd.Timestamp, end: pd.Timestamp) -> int:
    """Calendar days that can hold events in [start, end)."""
    if end <= start:
        return 1
    first = start.normalize()
    last = (end - pd.Timedelta(nanoseconds=1)).normalize()
    return max(1, int((last - first).days) + 1)


def faults_with_logs(alerts: list[Alert]) -> set[str]:
    return {alert.fault_id for alert in alerts if alert.fault_id}


def faults_with_jumps(faults: list[Fault], jumps: list[Spike]) -> set[str]:
    by_service: dict[str, list[Spike]] = defaultdict(list)
    for jump in jumps:
        by_service[jump.service].append(jump)
    caught: set[str] = set()
    for fault in faults:
        for jump in by_service.get(fault.component, []):
            if jump.start <= fault.end and jump.end >= fault.start:
                caught.add(fault.fault_id)
                break
    return caught


SIGNALS = ("log_and_jump", "jump_only", "log_only", "no_metric_data", "silent")


def signal_row(
    fault: Fault, logged: set[str], jumped: set[str], with_data: set[str] | None = None
) -> str:
    """`no_metric_data`: no log, no jump, and no metric point in the fault window
    (a monitoring gap). `silent`: metric data exists but nothing moved."""
    has_log = fault.fault_id in logged
    has_jump = fault.fault_id in jumped
    if has_log and has_jump:
        return "log_and_jump"
    if has_jump:
        return "jump_only"
    if has_log:
        return "log_only"
    if with_data is not None and fault.fault_id not in with_data:
        return "no_metric_data"
    return "silent"


def signal_counts(
    faults: list[Fault],
    alerts: list[Alert],
    jumps: list[Spike],
    with_data: set[str] | None = None,
) -> dict[str, int]:
    logged = faults_with_logs(alerts)
    jumped = faults_with_jumps(faults, jumps)
    counts = {key: 0 for key in SIGNALS}
    for fault in faults:
        counts[signal_row(fault, logged, jumped, with_data)] += 1
    return counts


def chance_overlap(
    faults: list[Fault], jumps: list[Spike], n_draws: int = 20, seed: int = 0
) -> tuple[float, float, float]:
    """Detection if each service's jumps were shifted by a random 1-6 hours.

    Tells a reader how much of the overlap rate a random detector would get.
    Returns (mean, min, max) over the draws.
    """
    rng = np.random.default_rng(seed)
    services = sorted({j.service for j in jumps})
    rates = []
    for _ in range(n_draws):
        shifted = []
        for service in services:
            offset = pd.Timedelta(minutes=int(rng.integers(60, 360)))
            shifted += [
                Spike(j.series_id, j.service, j.start + offset, j.end + offset, j.peak_rise)
                for j in jumps
                if j.service == service
            ]
        rates.append(len(faults_with_jumps(faults, shifted)) / len(faults) if faults else 0.0)
    return float(np.mean(rates)), float(min(rates)), float(max(rates))


def by_service_rows(
    faults: list[Fault], alerts: list[Alert], jumps: list[Spike]
) -> list[dict]:
    logged = faults_with_logs(alerts)
    jumped = faults_with_jumps(faults, jumps)
    services = sorted({fault.component for fault in faults})
    rows = []
    for service in services:
        part = [f for f in faults if f.component == service]
        n = len(part)
        n_log = sum(1 for f in part if f.fault_id in logged)
        n_jump = sum(1 for f in part if f.fault_id in jumped)
        rows.append(
            {
                "service": service,
                "faults": n,
                "with_log": n_log,
                "with_jump": n_jump,
                "log_rate": round(n_log / n, 4) if n else 0.0,
                "jump_rate": round(n_jump / n, 4) if n else 0.0,
            }
        )
    return rows


def week_pressure(faults: list[Fault], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Faults that start in [start, end), using exclusive-end calendar days."""
    part = [f for f in faults if start <= f.start < end]
    days = exclusive_window_days(start, end)
    return {
        "faults": len(part),
        "days": days,
        "faults_per_day": round(len(part) / days, 2),
    }


def jumps_starting_in(jumps: list[Spike], start: pd.Timestamp, end: pd.Timestamp) -> int:
    return sum(1 for jump in jumps if start <= jump.start < end)
