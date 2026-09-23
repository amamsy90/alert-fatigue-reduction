"""Score the whole on-call queue (surfaced alerts + shown memory jumps) on GAIA.

A fault counts as caught when a surfaced alert carries its label, or when a
shown jump on its service overlaps its injection window. Jumps are shown
biggest-first, at most K per day, exactly as the web app lists them.

Queues compared at each daily budget K:
  baseline           severity threshold + exact duplicates (K alerts)
  alerts             learned policy, ask only inside a jump (K alerts)
  jumps              biggest K jumps per day, no alerts
  alerts+jumps       K alerts and K jumps per day (up to 2K items)
  shared_alerts_1st  K items per day: surfaced alerts first, jumps fill the rest
  shared_jumps_1st   K items per day: jumps first, surfaced alerts fill the rest

The model, cost threshold and jump size are fixed on week 1; week 2 is only scored.

Usage:
    python run_queue_eval.py
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config
from src.entities import SURFACED_ACTIONS, Alert, Decision, Fault
from src.features import build_feature_frame
from src.gaia_loader import load_run_folder
from src.ingestion import time_split
from src.memory_spikes import Spike, detect_jumps, load_metric_folder
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold

# (day, rank key, kind, payload): kind is "alert" or "jump"
Item = tuple[pd.Timestamp, float, str, object]


def _alert_items(alerts: list[Alert], decisions: list[Decision]) -> list[Item]:
    return [
        (alert.timestamp.normalize(), float(decision.confidence), "alert", alert)
        for alert, decision in zip(alerts, decisions)
        if decision.action in SURFACED_ACTIONS
    ]


def _jump_items(jumps: list[Spike]) -> list[Item]:
    return [(jump.start.normalize(), jump.peak_rise, "jump", jump) for jump in jumps]


def _top_per_day(items: list[Item], k: int) -> list[Item]:
    by_day: dict[pd.Timestamp, list[Item]] = defaultdict(list)
    for item in items:
        by_day[item[0]].append(item)
    kept: list[Item] = []
    for day_items in by_day.values():
        day_items.sort(key=lambda item: -item[1])
        kept.extend(day_items[:k])
    return kept


def _shared(first: list[Item], second: list[Item], k: int) -> list[Item]:
    """Per day, take `first` (already capped at k) then fill leftover slots from `second`."""
    first = _top_per_day(first, k)
    used: dict[pd.Timestamp, int] = defaultdict(int)
    for item in first:
        used[item[0]] += 1
    by_day: dict[pd.Timestamp, list[Item]] = defaultdict(list)
    for item in second:
        by_day[item[0]].append(item)
    kept = list(first)
    for day, day_items in by_day.items():
        day_items.sort(key=lambda item: -item[1])
        kept.extend(day_items[: max(0, k - used[day])])
    return kept


def _score(name: str, k: int, shown: list[Item], faults: list[Fault], n_days: int) -> dict:
    faults_by_service: dict[str, list[Fault]] = defaultdict(list)
    for fault in faults:
        faults_by_service[fault.component].append(fault)
    fault_start = {fault.fault_id: fault.start for fault in faults}
    first_seen: dict[str, pd.Timestamp] = {}
    false_items = 0

    def _mark(fault_id: str, when: pd.Timestamp) -> None:
        if fault_id in fault_start and (fault_id not in first_seen or when < first_seen[fault_id]):
            first_seen[fault_id] = when

    for _, _, kind, payload in shown:
        if kind == "alert":
            if payload.fault_id in fault_start:
                _mark(payload.fault_id, payload.timestamp)
            else:
                false_items += 1
        else:
            hits = [
                fault
                for fault in faults_by_service.get(payload.service, [])
                if payload.start <= fault.end and payload.end >= fault.start
            ]
            for fault in hits:
                _mark(fault.fault_id, max(payload.start, fault.start))
            if not hits:
                false_items += 1

    delays = [
        (when - fault_start[fid]).total_seconds() / 60.0 for fid, when in first_seen.items()
    ]
    return {
        "queue": name,
        "budget_k": k,
        "faults": len(faults),
        "faults_caught": len(first_seen),
        "detection_rate": round(len(first_seen) / len(faults), 4) if faults else 0.0,
        "items_per_day": round(len(shown) / n_days, 2),
        "false_items_per_day": round(false_items / n_days, 2),
        "mtta_minutes": round(sum(delays) / len(delays), 2) if delays else None,
    }


def main() -> None:
    config = Config()
    alerts, faults = load_run_folder("data/gaia_raw/run/run")
    metrics = load_metric_folder("data/gaia_metrics")
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    _, test_faults = time_split(faults, config.split_day)
    origin = min(alert.timestamp for alert in alerts)
    split_at = origin + pd.Timedelta(days=config.split_day)

    train_jumps = detect_jumps(metrics, config, detect_from=origin, detect_until=split_at)
    test_jumps = detect_jumps(metrics, config, detect_from=split_at)
    model = AlertClassifier(config).fit(train_alerts, train_jumps)
    threshold = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_jumps), config)
    proba = model.predict_proba(test_alerts, test_jumps)
    in_jump = [bool(v) for v in build_feature_frame(test_alerts, config, test_jumps)["during_memory_spike"]]
    last = max(alert.timestamp for alert in test_alerts)
    n_days = (last.normalize() - split_at.normalize()).days + 1
    print(
        f"Week 2: {len(test_alerts)} alerts, {len(test_jumps)} jumps, "
        f"{len(test_faults)} faults over {n_days} days. Week-1 threshold {threshold:.3f}."
    )

    jump_items = _jump_items(test_jumps)
    rows = []
    for k in config.budget_sweep:
        cfg = Config(budget_k=k)
        base = _alert_items(test_alerts, baseline_decisions(test_alerts, cfg))
        learned = _alert_items(
            test_alerts,
            spike_review_decisions(
                test_alerts, proba, cfg, threshold, bayes_threshold(cfg), in_jump
            ),
        )
        queues = {
            "baseline": base,
            "alerts": learned,
            "jumps": _top_per_day(jump_items, k),
            "alerts+jumps": learned + _top_per_day(jump_items, k),
            "shared_alerts_1st": _shared(learned, jump_items, k),
            "shared_jumps_1st": _shared(jump_items, learned, k),
        }
        for name, shown in queues.items():
            row = _score(name, k, shown, test_faults, n_days)
            rows.append(row)
            print(
                f"K={k:2d}  {name:18s} caught {row['faults_caught']:4d}/{row['faults']} "
                f"({row['detection_rate']:.1%})  items/day {row['items_per_day']:5.1f}  "
                f"false/day {row['false_items_per_day']:5.1f}  mtta {row['mtta_minutes']}"
            )

    out = Path("results") / "queue_eval_gaia.csv"
    out.parent.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"Results written to {out}")


if __name__ == "__main__":
    main()
