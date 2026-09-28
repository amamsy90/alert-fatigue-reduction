"""The one definition of "caught" and the one threshold search, used everywhere.

A metric item catches a fault when it is on the fault's service and their time
windows overlap. A threshold is tuned by trying quantiles of the training
values and keeping the one with the lowest cost
(miss_cost x faults missed + false_alarm_cost x items that caught nothing).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Protocol

import numpy as np

from .config import Config
from .entities import Fault


class Episode(Protocol):
    """Anything with a service and a time window (a Spike)."""

    service: str
    start: object
    end: object


def overlaps(item: Episode, fault: Fault) -> bool:
    """Same time window, ignoring the service (callers match the service)."""
    return bool(item.start <= fault.end and item.end >= fault.start)


def faults_by_service(faults: Iterable[Fault]) -> dict[str, list[Fault]]:
    out: dict[str, list[Fault]] = defaultdict(list)
    for fault in faults:
        out[fault.component].append(fault)
    return out


def spike_hits(spike: Episode, by_service: dict[str, list[Fault]]) -> list[Fault]:
    """Faults on the spike's service whose window overlaps it."""
    return [f for f in by_service.get(spike.service, []) if overlaps(spike, f)]


def hit_sets(spikes: Sequence[Episode], faults: Iterable[Fault]) -> list[set[str]]:
    """For each spike, the ids of the faults it catches."""
    by_service = faults_by_service(faults)
    return [{f.fault_id for f in spike_hits(s, by_service)} for s in spikes]


def threshold_cost(values: Sequence[float], hits: Sequence[set[str]], n_faults: int, threshold: float,
                   config: Config) -> tuple[float, int, int]:
    """(cost, faults caught, false items) when only values >= threshold are shown."""
    caught: set[str] = set()
    false = 0
    for value, hit in zip(values, hits):
        if value < threshold:
            continue
        if hit:
            caught |= hit
        else:
            false += 1
    cost = config.miss_cost * (n_faults - len(caught)) + config.false_alarm_cost * false
    return cost, len(caught), false


def lowest_cost_threshold(values: Sequence[float], hits: Sequence[set[str]], n_faults: int, config: Config,
                          n_grid: int = 40) -> float | None:
    """The quantile of `values` (0 to 95%) with the lowest cost; the first wins a tie.
    None when there is nothing to tune."""
    if len(values) == 0:
        return None
    candidates = np.unique(np.quantile(np.asarray(values, dtype=float), np.linspace(0, 0.95, n_grid)))
    best: tuple[float, float] | None = None
    for thr in candidates:
        cost, _, _ = threshold_cost(values, hits, n_faults, float(thr), config)
        if best is None or cost < best[0]:
            best = (cost, float(thr))
    return best[1] if best else None
