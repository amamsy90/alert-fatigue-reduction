"""Score the held-out window's whole queue three ways, so the promotion gate tests the model
against what would actually run instead of it (see queue_check)."""
from __future__ import annotations

import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config
from src.entities import Alert, Decision, Fault
from src.features import build_feature_frame
from src.policy import spike_review_decisions
from src.queue import QueueItem, alert_items, build_queue, n_days_between, score_queue, spike_items

from .model_store import SavedModel
from .training_report import faults_from_labels


def spike_flags(alerts: list[Alert], spikes: list, config: Config) -> list[bool]:
    if not spikes:
        return [False] * len(alerts)
    frame = build_feature_frame(alerts, config, spikes)
    return [bool(v) for v in frame["during_memory_spike"]]


def apply_model(
    model: AlertClassifier,
    alerts: list[Alert],
    jumps: list,
    flags: list[bool],
    config: Config,
    escalate_threshold: float,
    review_threshold: float,
) -> list[Decision]:
    proba = model.predict_proba(alerts, jumps)
    return spike_review_decisions(alerts, proba, config, escalate_threshold, review_threshold, flags)


def queue_check(
    trained: dict,
    alerts: list[Alert],
    jumps: list,
    leaks: list,
    gaps: list,
    flags: list[bool],
    config: Config,
    faults: list[Fault] | None,
    fallback: SavedModel | None = None,
) -> dict | None:
    """The whole queue on the held-out window three ways: with this model's
    alerts, with the alerts of what scores the batch if the model is refused
    (`fallback`, the active model, or rules only = severity), and with no log
    alerts at all. A classifier can be right about single alerts and still
    lower the queue's catches, because the faults its alerts carry are often also
    caught by metric jumps and its alerts take slots the jumps needed (GAIA,
    docs/report_appendices_full.md section B4). None when the held-out window has no fault."""
    split_at = trained["split_at"]
    later = [i for i, a in enumerate(alerts) if a.timestamp >= split_at]
    if not later:
        return None
    later_alerts = [alerts[i] for i in later]
    held_faults = (
        [f for f in faults if f.start >= split_at] if faults else faults_from_labels(later_alerts)
    )
    if not held_faults:
        return None

    def metric_items() -> list[QueueItem]:  # fresh items per queue: build_queue marks them
        spikes = [s for s in jumps + leaks + gaps if s.shown_at >= split_at]
        return spike_items(spikes, config)

    decisions = apply_model(
        trained["model"], later_alerts, jumps, [flags[i] for i in later], config,
        trained["escalate_threshold"], trained["review_threshold"],
    )
    end = max(max(a.timestamp for a in later_alerts), max(f.end for f in held_faults)) + pd.Timedelta(seconds=1)
    n_days = n_days_between(split_at, end, config)
    later_flags = [flags[i] for i in later]
    if fallback is not None and fallback.model is not None:
        fallback_name = "the active model"
        fallback_decisions = apply_model(
            fallback.model, later_alerts, jumps, later_flags, config,
            fallback.escalate_threshold, fallback.review_threshold,
        )
    else:
        fallback_name = "rules only (severity)"
        fallback_decisions = baseline_decisions(later_alerts, config)
    out: dict = {"faults": len(held_faults), "days": n_days, "fallback": fallback_name}
    for name, items in (
        ("with_model", metric_items() + alert_items(later_alerts, decisions)),
        ("fallback", metric_items() + alert_items(later_alerts, fallback_decisions)),
        ("no_log_alerts", metric_items()),
    ):
        row = score_queue(name, build_queue(items, config), held_faults, n_days, config)
        false_items = int(round(row["false_items_per_day"] * n_days))
        missed = len(held_faults) - row["faults_caught"]
        out[f"caught_{name}"] = row["faults_caught"]
        out[f"false_items_{name}"] = false_items
        out[f"cost_{name}"] = config.miss_cost * missed + config.false_alarm_cost * false_items
    return out
