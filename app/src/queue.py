"""The causal on-call queue: what a person would see, in the order they would see it.

Every item is shown at the moment a live system could know about it:

* a jump at its confirmation point (`Spike.confirm`), if its rise by then is at
  least `config.confirm_min_rise`;
* a monitoring gap `gap_minutes` after the last point;
* a log alert at its own timestamp, if the alert policy surfaced it.

Each day holds at most `budget_k` items, first come first served. Log alerts
may use at most `alert_share` of the budget, so a noisy log stream cannot push
out a metric jump that arrives later. Nothing here looks at a later point or
`Spike.peak_rise`. An alert rides on a jump only while `Spike.open_at` says a
live system still sees that jump open, which needs points up to the alert only.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config, day_of
from .entities import SURFACED_ACTIONS, Alert, Decision, Fault
from .memory_spikes import Spike
from .scoring import faults_by_service as _faults_by_service
from .scoring import hit_sets, lowest_cost_threshold, spike_hits  # noqa: F401 — spike_hits is re-exported


@dataclass
class QueueItem:
    shown_at: pd.Timestamp
    kind: str  # "jump" | "gap" | "rule" | "alert"
    service: str
    payload: Spike | Alert
    # An alert on a service whose shown jump is still running rides on that jump:
    # it is shown inside the jump item and uses no budget slot.
    attached: bool = False

    @property
    def spike(self) -> Spike:
        """The metric episode of a jump, leak, rule or gap item."""
        if not isinstance(self.payload, Spike):
            raise TypeError(f"{self.kind} item has no spike")
        return self.payload

    @property
    def alert(self) -> Alert:
        """The log alert of an alert item."""
        if not isinstance(self.payload, Alert):
            raise TypeError(f"{self.kind} item has no alert")
        return self.payload


def confirm_gate(spike: Spike, config: Config) -> float | None:
    """Rise a jump must reach before the queue shows it.

    A series with a threshold learned on the training window uses that frozen
    number. Every other series uses the global `confirm_min_rise` (or none).
    """
    table = config.confirm_min_rise_by_series or {}
    if spike.service in table:
        return table[spike.service]
    if spike.series_id in table:
        return table[spike.series_id]
    return config.confirm_min_rise


def spike_items(spikes: list[Spike], config: Config, use_threshold: bool = True) -> list[QueueItem]:
    items = []
    for spike in spikes:
        if use_threshold and spike.kind == "jump":
            thr = confirm_gate(spike, config)
            if thr is not None and spike.rise_at_confirm < thr:
                continue
        items.append(QueueItem(spike.shown_at, spike.kind, spike.service, spike))
    return items


def alert_items(alerts: list[Alert], decisions: list[Decision]) -> list[QueueItem]:
    return [
        QueueItem(alert.timestamp, "alert", alert.source, alert)
        for alert, decision in zip(alerts, decisions)
        if decision.action in SURFACED_ACTIONS
    ]


METRIC_KINDS = ("jump", "rule", "leak")
# At one instant, a jump/leak/rule is decided before a gap, and a gap before an
# alert. Otherwise an alert that arrives at the confirmation point takes the
# last slot and the jump it belongs to is dropped.
_KIND_ORDER = {"jump": 0, "leak": 1, "rule": 2, "gap": 3, "alert": 4}


def build_queue(items: list[QueueItem], config: Config) -> list[QueueItem]:
    """First come, first served, at most K slots a day.

    * Log alerts may use at most `alert_share * K` slots a day, gap items `gap_share * K`.
    * An alert that arrives while a shown jump on its service is still running is
      attached to that jump instead of taking a slot (one incident, one item).
    * `group_minutes` > 0: a metric item confirmed within that many minutes after
      a shown metric item on ANOTHER service is attached to it (one incident that
      touches several services). Off by default.
    * `carry_over_max` > 0: slots unused yesterday are added to today's budget,
      up to that many. The long-run average stays at most K a day. Off by default.
    """
    k = config.budget_k
    alert_cap = int(math.floor(k * config.alert_share))
    gap_cap = int(math.floor(k * config.gap_share))
    group = pd.Timedelta(minutes=config.group_minutes)
    by_day: dict[pd.Timestamp, list[QueueItem]] = defaultdict(list)
    for item in items:
        by_day[day_of(item.shown_at, config)].append(item)
    shown: list[QueueItem] = []
    running: dict[str, list[Spike]] = defaultdict(list)  # shown metric items per service
    last_group: QueueItem | None = None  # latest shown metric item that took a slot
    carry = 0
    days = sorted(by_day)
    all_days = pd.date_range(days[0], days[-1], freq="D") if days else []
    for day in all_days:
        today = k + carry
        used = alerts_used = gaps_used = 0
        for item in sorted(by_day.get(day, []), key=lambda it: (it.shown_at, _KIND_ORDER.get(it.kind, 5))):
            if item.kind == "alert":
                jumps = running.get(item.service, [])
                if any(j.open_at(item.shown_at) for j in jumps):
                    item.attached = True
                    shown.append(item)
                    continue
                if used >= today or alerts_used >= alert_cap:
                    continue
                alerts_used += 1
            elif item.kind in METRIC_KINDS:
                if (
                    config.group_minutes > 0
                    and last_group is not None
                    and item.service != last_group.service
                    and item.shown_at - last_group.shown_at <= group
                ):
                    item.attached = True
                    shown.append(item)
                    running[item.service].append(item.spike)
                    continue
                if used >= today:
                    continue
            elif item.kind == "gap":
                if used >= today or gaps_used >= gap_cap:
                    continue
                gaps_used += 1
            elif used >= today:
                continue
            used += 1
            shown.append(item)
            if item.kind in METRIC_KINDS:
                running[item.service].append(item.spike)
                last_group = item
        carry = min(config.carry_over_max, max(0, today - used)) if config.carry_over_max > 0 else 0
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
    useful = 0  # slot items (not gaps) that caught at least one fault
    for item in shown:
        slots += not item.attached
        if item.kind == "gap":
            gap_items += 1
            services = set(item.service.split(","))
            for f in faults:
                if f.component in services and item.spike.start <= f.start <= item.spike.end:
                    blind.add(f.fault_id)
            continue
        if item.kind == "alert":
            fid = item.alert.fault_id
            hit_ids = [fid] if fid is not None and fid in known else []
        else:
            hit_ids = [f.fault_id for f in spike_hits(item.spike, by_service)]
        if not hit_ids and not item.attached:
            false_items += 1
        if hit_ids and not item.attached:
            useful += 1
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
        # Of the items that took a slot (gaps excluded), the share that caught a fault.
        "precision": round(useful / (useful + false_items), 4) if useful + false_items else 0.0,
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
    rises = [j.rise_at_confirm for j in jumps]
    return lowest_cost_threshold(rises, hit_sets(jumps, faults), len(faults), config, n_grid)


def tune_confirm_thresholds(
    jumps: list[Spike],
    faults: list[Fault],
    config: Config,
    *,
    train_until: pd.Timestamp | None = None,
    min_jumps: int = 3,
) -> tuple[float | None, dict[str, float]]:
    """Global confirm rise, plus one threshold per series, both from the training window.

    Pass `train_until` to ignore jumps and faults at or after that time, so a
    test-window point cannot move the number. A series with fewer than
    `min_jumps` training jumps, or with no training fault of its own, is left
    out of the map and keeps the global threshold (the current single gate).
    The cost is the same one `tune_confirm_threshold` minimises.
    """
    if train_until is not None:
        jumps = [j for j in jumps if j.shown_at < train_until]
        faults = [f for f in faults if f.start < train_until]
    global_thr = tune_confirm_threshold(jumps, faults, config)
    own_faults = _faults_by_service(faults)
    grouped: dict[str, list[Spike]] = defaultdict(list)
    for jump in jumps:
        if jump.kind == "jump":
            grouped[jump.service].append(jump)
    by_series: dict[str, float] = {}
    for service, series_jumps in grouped.items():
        series_faults = own_faults.get(service, [])
        if len(series_jumps) < min_jumps or not series_faults:
            continue
        thr = tune_confirm_threshold(series_jumps, series_faults, config)
        if thr is not None:
            by_series[service] = thr
    return global_thr, by_series


def n_days_between(start: pd.Timestamp, end: pd.Timestamp, config: Config) -> int:
    """Calendar days touched by [start, end)."""
    first = day_of(start, config)
    last = day_of(end - pd.Timedelta(nanoseconds=1), config)
    return max(1, int((last - first).days) + 1)
