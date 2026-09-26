"""The causal on-call queue: what a person would see, in the order they would see it.

Every item is shown at the moment a live system could know about it:

* a jump at its confirmation point (`Spike.confirm`), if its rise by then is at
  least `config.confirm_min_rise`;
* a monitoring gap `gap_minutes` after the last point;
* a log alert at its own timestamp, if the alert policy surfaced it.

Each day holds at most `budget_k` items, first come first served. Log alerts
may use at most `alert_share` of the budget, so a noisy log stream cannot push
out a metric jump that arrives later. Nothing here looks at a later point,
`Spike.end` or `Spike.peak_rise`.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config
from .entities import SURFACED_ACTIONS, Alert, Decision, Fault
from .memory_spikes import Spike


@dataclass
class QueueItem:
    shown_at: pd.Timestamp
    kind: str  # "jump" | "gap" | "rule" | "alert"
    service: str
    payload: object  # Spike or Alert
    # An alert on a service whose shown jump is still running rides on that jump:
    # it is shown inside the jump item and uses no budget slot.
    attached: bool = False


def day_of(ts: pd.Timestamp, config: Config) -> pd.Timestamp:
    """Calendar day of `ts` in the on-call's clock (UTC + day_offset_hours)."""
    return (ts + pd.Timedelta(hours=config.day_offset_hours)).normalize()


def spike_items(spikes: list[Spike], config: Config, use_threshold: bool = True) -> list[QueueItem]:
    thr = config.confirm_min_rise if use_threshold else None
    items = []
    for spike in spikes:
        if spike.kind == "jump" and thr is not None and spike.rise_at_confirm < thr:
            continue
        items.append(QueueItem(spike.shown_at, spike.kind, spike.service, spike))
    return items


def alert_items(alerts: list[Alert], decisions: list[Decision]) -> list[QueueItem]:
    return [
        QueueItem(alert.timestamp, "alert", alert.source, alert)
        for alert, decision in zip(alerts, decisions)
        if decision.action in SURFACED_ACTIONS
    ]


def build_queue(items: list[QueueItem], config: Config) -> list[QueueItem]:
    """First come, first served, at most K slots a day.

    * Log alerts may use at most `alert_share * K` slots a day.
    * An alert that arrives while a shown jump on its service is still running is
      attached to that jump instead of taking a slot (one incident, one item).
    """
    k = config.budget_k
    alert_cap = int(math.floor(k * config.alert_share))
    by_day: dict[pd.Timestamp, list[QueueItem]] = defaultdict(list)
    for item in items:
        by_day[day_of(item.shown_at, config)].append(item)
    shown: list[QueueItem] = []
    running: dict[str, list[Spike]] = defaultdict(list)  # shown jumps per service
    for day in sorted(by_day):
        used = alerts_used = 0
        for item in sorted(by_day[day], key=lambda it: (it.shown_at, it.kind)):
            if item.kind == "alert":
                jumps = running.get(item.service, [])
                if any(j.shown_at <= item.shown_at <= j.end for j in jumps):
                    item.attached = True
                    shown.append(item)
                    continue
                if used >= k or alerts_used >= alert_cap:
                    continue
                alerts_used += 1
            elif used >= k:
                continue
            used += 1
            shown.append(item)
            if item.kind in ("jump", "rule"):
                running[item.service].append(item.payload)
    return shown


def hindsight_top_k(spikes: list[Spike], config: Config) -> list[QueueItem]:
    """The OLD queue: each day's jumps ranked by final peak. Needs the whole day
    in advance, so it is only kept to show how much hindsight was worth."""
    by_day: dict[pd.Timestamp, list[Spike]] = defaultdict(list)
    for spike in spikes:
        by_day[day_of(spike.start, config)].append(spike)
    out = []
    for day_spikes in by_day.values():
        for spike in sorted(day_spikes, key=lambda s: -s.peak_rise)[: config.budget_k]:
            out.append(QueueItem(spike.start, spike.kind, spike.service, spike))
    return out


# ---------------------------------------------------------------------------
# Scoring


def _faults_by_service(faults: list[Fault]) -> dict[str, list[Fault]]:
    out: dict[str, list[Fault]] = defaultdict(list)
    for fault in faults:
        out[fault.component].append(fault)
    return out


def spike_hits(spike: Spike, by_service: dict[str, list[Fault]]) -> list[Fault]:
    return [
        f
        for f in by_service.get(spike.service, [])
        if spike.start <= f.end and spike.end >= f.start
    ]


def score_queue(
    name: str,
    shown: list[QueueItem],
    faults: list[Fault],
    n_days: int,
    config: Config,
) -> dict:
    """Caught = a shown jump/rule item overlaps the fault on its service, or a
    shown alert carries its fault id. Gap items never count as catches; they are
    reported as `blind_faults_flagged` (faults that started inside a shown gap)."""
    by_service = _faults_by_service(faults)
    known = {f.fault_id: f for f in faults}
    first: dict[str, pd.Timestamp] = {}
    false_items = gap_items = 0
    blind: set[str] = set()
    slots = 0
    for item in shown:
        slots += not item.attached
        if item.kind == "gap":
            gap_items += 1
            services = set(item.service.split(","))
            for f in faults:
                if f.component in services and item.payload.start <= f.start <= item.payload.end:
                    blind.add(f.fault_id)
            continue
        if item.kind == "alert":
            hit_ids = [item.payload.fault_id] if item.payload.fault_id in known else []
        else:
            hit_ids = [f.fault_id for f in spike_hits(item.payload, by_service)]
        if not hit_ids and not item.attached:
            false_items += 1
        for fid in hit_ids:
            if fid not in first or item.shown_at < first[fid]:
                first[fid] = item.shown_at
    delays = [(first[fid] - known[fid].start).total_seconds() / 60 for fid in first]
    return {
        "queue": name,
        "budget_k": config.budget_k,
        "faults": len(faults),
        "faults_caught": len(first),
        "detection_rate": round(len(first) / len(faults), 4) if faults else 0.0,
        "items_per_day": round(slots / n_days, 2),
        "attached_alerts": sum(1 for item in shown if item.attached),
        "false_items_per_day": round(false_items / n_days, 2),
        "gap_items_per_day": round(gap_items / n_days, 2),
        "blind_faults_flagged": len(blind),
        "confirm_minutes_median": round(float(np.median(delays)), 2) if delays else None,
        "confirm_minutes_p90": round(float(np.percentile(delays, 90)), 2) if delays else None,
        "cost_per_day": round(
            (config.miss_cost * (len(faults) - len(first)) + config.false_alarm_cost * false_items)
            / n_days,
            1,
        ),
    }


def oracle_ceiling(faults: list[Fault], config: Config) -> float:
    """Best possible detection with K items a day: every item is a distinct fault."""
    if not faults:
        return 0.0
    per_day = pd.Series([day_of(f.start, config) for f in faults]).value_counts()
    return float(per_day.clip(upper=config.budget_k).sum() / len(faults))


def tune_confirm_threshold(
    jumps: list[Spike], faults: list[Fault], config: Config, n_grid: int = 40
) -> float | None:
    """Rise-at-confirmation threshold with the lowest training cost
    (miss_cost x missed faults + false_alarm_cost x jumps with no fault).

    Candidates are quantiles of the training rises, so the same code works in
    absolute (bytes) and relative mode. Returns None when there is nothing to tune.
    """
    jumps = [j for j in jumps if j.kind == "jump"]
    if not jumps or not faults:
        return None
    by_service = _faults_by_service(faults)
    hits = [{f.fault_id for f in spike_hits(j, by_service)} for j in jumps]
    rises = np.array([j.rise_at_confirm for j in jumps])
    candidates = np.unique(np.quantile(rises, np.linspace(0, 0.95, n_grid)))
    best: tuple[float, float] | None = None
    for thr in candidates:
        caught: set[str] = set()
        false = 0
        for rise, hit in zip(rises, hits):
            if rise < thr:
                continue
            if hit:
                caught |= hit
            else:
                false += 1
        cost = config.miss_cost * (len(faults) - len(caught)) + config.false_alarm_cost * false
        if best is None or cost < best[0]:
            best = (cost, float(thr))
    return best[1] if best else None


def n_days_between(start: pd.Timestamp, end: pd.Timestamp, config: Config) -> int:
    """Calendar days touched by [start, end)."""
    first = day_of(start, config)
    last = day_of(end - pd.Timedelta(nanoseconds=1), config)
    return max(1, int((last - first).days) + 1)
