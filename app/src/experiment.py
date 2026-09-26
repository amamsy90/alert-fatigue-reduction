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
from .jump_ranker import JumpRanker
from .memory_spikes import Spike, detect_gaps, detect_jumps, detect_leaks, load_metric_folder
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
    tune_confirm_thresholds,
)

# Order is the order of rows in the result tables.
SYSTEMS = (
    "logs_severity",      # usual rule: log severity + exact duplicates
    "static_p99",         # memory > training p99 (a static Prometheus rule)
    "delta_5m",           # delta(mem[5m]) > 0.5 GB (a rate Prometheus rule) — the fair baseline
    "jumps_hindsight",    # OLD headline: day's jumps ranked by final peak (not causal)
    "jumps_online",       # jump rule, first come, no threshold
    "jumps_tuned",        # + week-1 confirm threshold (metric only)
    "proposed",           # jumps_tuned + leak + gap items + alerts (<= alert_share of K; alerts inside a shown jump ride on it)
    "p3_ranker",          # Phase 3: proposed, but a learned model (not the rise threshold) decides which jumps to show
    "p3_grouped",         # Phase 3: proposed + items within 2 min on other services share one slot
    "p3_carryover",       # Phase 3: proposed + up to 10 unused slots carried to the next day
    "delta_5m_carryover", # fair partner for p3_carryover: the rate rule with the same carry-over
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
    # Per-series confirm gates learned on the training window. Empty means every
    # series used the single global threshold (the published GAIA tables).
    confirm_by_series: dict | None = None


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
    train_leaks = detect_leaks(data.metrics, config, detect_until=train_end)
    test_leaks = detect_leaks(data.metrics, config, detect_from=train_end, detect_until=test_end)
    # Week-1 only. Series with enough of their own faults get a frozen gate;
    # the rest keep the single global threshold. Score time does not refit.
    thr, by_series = tune_confirm_thresholds(train_jumps, train_faults, config, train_until=train_end)
    tuned = replace(config, confirm_min_rise=thr, confirm_min_rise_by_series=by_series)

    model = AlertClassifier(config).fit(train_alerts, train_jumps)
    alert_thr = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_jumps), config)
    proba = model.predict_proba(test_alerts, test_jumps)
    in_jump = [bool(v) for v in build_feature_frame(test_alerts, config, test_jumps)["during_memory_spike"]]
    learned_decisions = spike_review_decisions(
        test_alerts, proba, config, alert_thr, bayes_threshold(config), in_jump
    )

    def learned() -> list[QueueItem]:
        # Fresh items per queue: build_queue marks items as attached.
        return alert_items(test_alerts, learned_decisions)

    logs = alert_items(test_alerts, baseline_decisions(test_alerts, config))
    static = detect_static(data.metrics, config, fit_until=train_end, detect_from=train_end, detect_until=test_end)
    delta = detect_delta(data.metrics, config, detect_from=train_end, detect_until=test_end)
    all_alerts = replace(config, alert_share=1.0)
    proposed_items = lambda: (  # noqa: E731 — fresh items each time (build_queue marks them)
        spike_items(test_jumps, tuned) + spike_items(test_leaks, tuned) + spike_items(test_gaps, tuned) + learned()
    )
    ranker = JumpRanker(config).fit(train_jumps + train_leaks, train_alerts, train_faults)
    history = [a for a in data.alerts if a.timestamp < test_end]
    ranked_metric = ranker.keep(test_jumps + test_leaks + test_gaps, history)

    shown = {
        "logs_severity": build_queue(logs, all_alerts),
        "static_p99": build_queue(spike_items(static, config), config),
        "delta_5m": build_queue(spike_items(delta, config), config),
        "jumps_hindsight": hindsight_top_k(test_jumps, config),
        "jumps_online": build_queue(spike_items(test_jumps, config, use_threshold=False), config),
        "jumps_tuned": build_queue(spike_items(test_jumps, tuned), tuned),
        "proposed": build_queue(proposed_items(), tuned),
        "p3_ranker": build_queue(spike_items(ranked_metric, config, use_threshold=False) + learned(), config),
        "p3_grouped": build_queue(proposed_items(), replace(tuned, group_minutes=2)),
        "p3_carryover": build_queue(proposed_items(), replace(tuned, carry_over_max=10)),
        "delta_5m_carryover": build_queue(spike_items(delta, config), replace(config, carry_over_max=10)),
    }
    return WindowRun(
        shown=shown,
        test_faults=test_faults,
        n_days=n_days_between(train_end, test_end, config),
        confirm_threshold=thr,
        alert_threshold=alert_thr,
        test_jumps=test_jumps,
        test_gaps=test_gaps,
        confirm_by_series=by_series,
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


MISS_CAUSES = ("no_metric_data", "no_jump", "below_threshold", "budget_full")


def miss_causes(run: WindowRun, data: GaiaData, config: Config, name: str = "proposed") -> dict[str, int]:
    """Why each fault the queue `name` did not catch was missed.

    no_metric_data   the service sent no metric point during the fault
    no_jump          data exists, but no jump overlapped the fault
    below_threshold  a jump overlapped, but its rise at confirmation was under the threshold
    budget_full      a qualifying jump existed, but the day's K slots were already used
    """
    from .queue import spike_hits

    by_service: dict[str, list[Fault]] = {}
    for fault in run.test_faults:
        by_service.setdefault(fault.component, []).append(fault)
    shown = run.shown[name]
    caught: set[str] = set()
    for item in shown:
        if item.kind == "alert":
            if item.payload.fault_id:
                caught.add(item.payload.fault_id)
        elif item.kind != "gap":
            caught |= {f.fault_id for f in spike_hits(item.payload, by_service)}
    with_data = faults_with_metric_data(run.test_faults, data.metrics)
    use_gate = name in ("proposed", "jumps_tuned")
    table = run.confirm_by_series or {}
    overlap: dict[str, list[Spike]] = {}
    for jump in run.test_jumps:
        for fault in spike_hits(jump, by_service):
            overlap.setdefault(fault.fault_id, []).append(jump)
    counts = {cause: 0 for cause in MISS_CAUSES}
    for fault in run.test_faults:
        if fault.fault_id in caught:
            continue
        jumps = overlap.get(fault.fault_id, [])
        if fault.fault_id not in with_data:
            counts["no_metric_data"] += 1
        elif not jumps:
            counts["no_jump"] += 1
        elif use_gate and jumps and all(
            (gate := table.get(j.service, table.get(j.series_id, run.confirm_threshold))) is not None
            and j.rise_at_confirm < gate
            for j in jumps
        ):
            counts["below_threshold"] += 1
        else:
            counts["budget_full"] += 1
    return counts
