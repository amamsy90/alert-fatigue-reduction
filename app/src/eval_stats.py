"""Numbers a marker can defend: intervals, a test vs baseline, and why faults are missed."""
from __future__ import annotations

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
    """Two-sided-style binomial: P(this many wins) if each fold is a coin flip.

    Ties are dropped. Used to say the queue beat the baseline in every later week.
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
    p = float(2 ** (-n)) if n else 1.0
    return SignTest(wins=wins, n=n, p_value=p)


def jump_overlaps(jump: Spike, fault: Fault) -> bool:
    return jump.service == fault.component and jump.start <= fault.end and jump.end >= fault.start


def fault_has_log(fault: Fault, alerts: list[Alert]) -> bool:
    return any(alert.fault_id == fault.fault_id for alert in alerts)


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


def signal_row(fault: Fault, logged: set[str], jumped: set[str]) -> str:
    has_log = fault.fault_id in logged
    has_jump = fault.fault_id in jumped
    if has_log and has_jump:
        return "log_and_jump"
    if has_jump:
        return "jump_only"
    if has_log:
        return "log_only"
    return "silent"


def signal_counts(faults: list[Fault], alerts: list[Alert], jumps: list[Spike]) -> dict[str, int]:
    logged = faults_with_logs(alerts)
    jumped = faults_with_jumps(faults, jumps)
    counts = {"log_and_jump": 0, "jump_only": 0, "log_only": 0, "silent": 0}
    for fault in faults:
        counts[signal_row(fault, logged, jumped)] += 1
    return counts


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
    """How hard the budget is in one week: faults per day vs K=20."""
    part = [f for f in faults if start <= f.start < end]
    days = max(1, (end.normalize() - start.normalize()).days)
    return {
        "faults": len(part),
        "days": days,
        "faults_per_day": round(len(part) / days, 2),
    }
