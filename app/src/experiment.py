"""One train/test window, every system, one scorer. Used by run_queue_eval,
run_walkforward and run_stats so all published numbers come from the same code.

Everything learned (classifier, cost threshold, jump confirmation threshold,
static-rule quantile) sees only data before `train_end`.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

from .baseline import baseline_decisions
from .baselines_metric import detect_delta, detect_static
from .classifier import AlertClassifier
from .config import Config
from .entities import Alert, Fault
from .features import build_feature_frame
from .gaia_loader import load_run_folder
from .memory_spikes import Spike, detect_gaps, detect_jumps, load_metric_folder
from .policy import bayes_threshold, spike_review_decisions, tune_threshold
from .queue import (
    QueueItem,
    alert_items,
    build_queue,
    hindsight_top_k,
    n_days_between,
    oracle_ceiling,
    score_queue,
    spike_items,
    tune_confirm_threshold,
)

# Order is the order of rows in the result tables.
SYSTEMS = (
    "logs_severity",      # usual rule: log severity + exact duplicates
    "static_p99",         # memory > training p99 (a static Prometheus rule)
    "delta_5m",           # delta(mem[5m]) > 0.5 GB (a rate Prometheus rule) — the fair baseline
    "jumps_hindsight",    # OLD headline: day's jumps ranked by final peak (not causal)
    "jumps_online",       # jump rule, first come, no threshold
    "jumps_tuned",        # + week-1 confirm threshold (metric only)
    "proposed",           # jumps_tuned + gap items + alerts (<= alert_share of K; alerts inside a shown jump ride on it)
)


@dataclass
class GaiaData:
    alerts: list[Alert]
    faults: list[Fault]
    metrics: pd.DataFrame
    origin: pd.Timestamp
    last: pd.Timestamp


def load_gaia(run_dir: str = "data/gaia_raw/run/run", metrics_dir: str = "data/gaia_metrics") -> GaiaData:
    alerts, faults = load_run_folder(run_dir)
    metrics = load_metric_folder(metrics_dir)
    return GaiaData(
        alerts=alerts,
        faults=faults,
        metrics=metrics,
        origin=min(a.timestamp for a in alerts),
        last=max(a.timestamp for a in alerts),
    )


@dataclass
class WindowRun:
    shown: dict[str, list[QueueItem]]
    test_faults: list[Fault]
    n_days: int
    confirm_threshold: float | None
    alert_threshold: float
    test_jumps: list[Spike]
    test_gaps: list[Spike]


def _between(items, start, end, attr):
    return [x for x in items if start <= getattr(x, attr) < end]


def run_window(
    data: GaiaData,
    train_end: pd.Timestamp,
    test_end: pd.Timestamp,
    config: Config,
) -> WindowRun:
    """Train on [origin, train_end), score [train_end, test_end) with every system."""
    train_alerts = [a for a in data.alerts if a.timestamp < train_end]
    test_alerts = _between(data.alerts, train_end, test_end, "timestamp")
    train_faults = [f for f in data.faults if f.start < train_end]
    test_faults = _between(data.faults, train_end, test_end, "start")

    train_jumps = detect_jumps(data.metrics, config, detect_until=train_end)
    test_jumps = detect_jumps(data.metrics, config, detect_from=train_end, detect_until=test_end)
    test_gaps = detect_gaps(data.metrics, config, detect_from=train_end, detect_until=test_end)
    thr = tune_confirm_threshold(train_jumps, train_faults, config)
    tuned = replace(config, confirm_min_rise=thr)

    model = AlertClassifier(config).fit(train_alerts, train_jumps)
    alert_thr = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_jumps), config)
    proba = model.predict_proba(test_alerts, test_jumps)
    in_jump = [bool(v) for v in build_feature_frame(test_alerts, config, test_jumps)["during_memory_spike"]]
    learned = alert_items(
        test_alerts,
        spike_review_decisions(test_alerts, proba, config, alert_thr, bayes_threshold(config), in_jump),
    )
    logs = alert_items(test_alerts, baseline_decisions(test_alerts, config))
    static = detect_static(data.metrics, config, fit_until=train_end, detect_from=train_end, detect_until=test_end)
    delta = detect_delta(data.metrics, config, detect_from=train_end, detect_until=test_end)
    all_alerts = replace(config, alert_share=1.0)

    shown = {
        "logs_severity": build_queue(logs, all_alerts),
        "static_p99": build_queue(spike_items(static, config), config),
        "delta_5m": build_queue(spike_items(delta, config), config),
        "jumps_hindsight": hindsight_top_k(test_jumps, config),
        "jumps_online": build_queue(spike_items(test_jumps, config, use_threshold=False), config),
        "jumps_tuned": build_queue(spike_items(test_jumps, tuned), tuned),
        "proposed": build_queue(
            spike_items(test_jumps, tuned) + spike_items(test_gaps, tuned) + learned, tuned
        ),
    }
    return WindowRun(
        shown=shown,
        test_faults=test_faults,
        n_days=n_days_between(train_end, test_end, config),
        confirm_threshold=thr,
        alert_threshold=alert_thr,
        test_jumps=test_jumps,
        test_gaps=test_gaps,
    )


def score_window(run: WindowRun, config: Config) -> list[dict]:
    rows = [
        score_queue(name, run.shown[name], run.test_faults, run.n_days, config) for name in SYSTEMS
    ]
    ceiling = oracle_ceiling(run.test_faults, config)
    for row in rows:
        row["oracle_ceiling"] = round(ceiling, 4)
        row["share_of_ceiling"] = round(row["detection_rate"] / ceiling, 4) if ceiling else 0.0
        row["confirm_threshold"] = run.confirm_threshold
    return rows


def faults_with_metric_data(faults: list[Fault], metrics: pd.DataFrame) -> set[str]:
    """Faults whose service has at least one metric point inside the fault window."""
    times = {
        service: part["timestamp"].dt.tz_convert(None).sort_values().to_numpy()
        for service, part in metrics.groupby("service")
    }
    have: set[str] = set()
    for fault in faults:
        arr = times.get(fault.component)
        if arr is None or len(arr) == 0:
            continue
        lo = arr.searchsorted(fault.start.tz_convert(None).to_datetime64())
        if lo < len(arr) and arr[lo] <= fault.end.tz_convert(None).to_datetime64():
            have.add(fault.fault_id)
    return have
