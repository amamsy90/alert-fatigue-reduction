"""Time-split comparison of the trace model, the jump baseline, and the queue.

The trace model names one service per fault window. A catch counts only when
that service is the fault's root-cause component. The non-learned baseline
names the service with the largest error-rate or latency jump in the same
window. The queue is the current causal queue at each K.

A variant is kept only when the 95% bootstrap interval on the per-fault catch
difference lies entirely above zero. The trace model is kept only when that is
true against both the baseline and the queue, at K = 20. This module does not
turn ``TRACE_MODEL`` on and it does not invent GAIA or RCAEval numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from src.config import Config
from src.entities import Alert, Fault
from src.eval_stats import bootstrap_mean_ci
from src.memory_spikes import detect_gaps, detect_jumps, detect_leaks
from src.queue import (
    alert_items,
    build_queue,
    n_days_between,
    score_queue,
    spike_items,
    tune_confirm_thresholds,
)
from src.ranker import caught_fault_ids

from .trace_graph import CallGraph, baseline_service, build_graph, root_cause_hit, with_baseline_feature
from .trace_model import MIN_WINDOWS, TraceArchive, _window_end, caught_ids, fit_trace_model, predict_services

ORIGIN = pd.Timestamp("2026-01-05", tz="UTC")
# RE2 keeps the day load_cases assigned. RE3 starts this many days later so the
# two suites do not write the same series at the same timestamp.
SUITE_DAY_BLOCK = 1000
_SUITE_INDEX = {"RE2": 0, "RE3": 1}
# A slice with fewer training cases than this is flagged. It is still scored.
TINY_TRAIN_CASES = 20


def suite_day(local_day: int, suite: str, *, block: int = SUITE_DAY_BLOCK) -> int:
    """Day index that keeps one suite off another's clock.

    Within a suite the local day is unchanged, so Online Boutique and Train
    Ticket still share a day and compete for the same K.
    """
    try:
        index = _SUITE_INDEX[suite]
    except KeyError as exc:
        raise ValueError(f"suite must be RE2 or RE3, not {suite!r}") from exc
    return index * block + local_day


def tiny_train(train_cases: int, *, floor: int = TINY_TRAIN_CASES) -> bool:
    """True when a split has fewer than ``floor`` training cases."""
    return train_cases < floor


@dataclass
class TraceSpec:
    hidden: int = 8
    epochs: int = 20
    seed: int = 42
    device: str = "cpu"
    backend: str = "plain"
    budget_ks: tuple[int, ...] = (5, 10, 20, 50)
    headline_k: int = 20


def _utc(stamp: pd.Timestamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def model_skip_reason(status: str) -> str:
    """A sentence for a slice that was not fit. Other statuses pass through."""
    if str(status).startswith("too-few-windows"):
        return (
            f"skipped ({status}): a model is fit only when at least {MIN_WINDOWS} "
            "training windows contain the root-cause service. This slice was not scored."
        )
    return status


def _decision(base: set[str], cand: set[str], faults: list[Fault], seed: int) -> dict:
    if not faults:
        return {
            "status": "skipped",
            "decision": "skipped",
            "reason": "no held-out faults",
            "per_fault_catch_delta": None,
        }
    delta = [float(fault.fault_id in cand) - float(fault.fault_id in base) for fault in faults]
    interval = bootstrap_mean_ci(delta, seed=seed)
    kept = interval.low > 0.0
    return {
        "status": "scored",
        "decision": "keep" if kept else "drop",
        "reason": (
            "the 95% interval for the per-fault catch difference lies above zero"
            if kept
            else "the 95% interval for the per-fault catch difference does not lie above zero"
        ),
        "per_fault_catch_delta": {
            "mean": round(interval.mean, 4),
            "low": round(interval.low, 4),
            "high": round(interval.high, 4),
            "n": interval.n,
        },
        "faults_caught": len(cand),
        "faults": len(faults),
    }


def _queue_scored(
    metrics: pd.DataFrame,
    alerts: list[Alert],
    faults: list[Fault],
    config: Config,
    boundary: pd.Timestamp,
    stop: pd.Timestamp,
) -> tuple[set[str], dict]:
    train_faults = [fault for fault in faults if _utc(fault.start) < boundary]
    train_jumps = detect_jumps(metrics, config, detect_until=boundary)
    test_jumps = detect_jumps(metrics, config, detect_from=boundary, detect_until=stop)
    test_gaps = detect_gaps(metrics, config, detect_from=boundary, detect_until=stop)
    test_leaks = detect_leaks(metrics, config, detect_from=boundary, detect_until=stop)
    threshold, by_series = tune_confirm_thresholds(train_jumps, train_faults, config, train_until=boundary)
    tuned = replace(config, confirm_min_rise=threshold, confirm_min_rise_by_series=by_series)
    items = spike_items(test_jumps, tuned) + spike_items(test_leaks, tuned) + spike_items(test_gaps, tuned)
    if alerts:
        from src.baseline import baseline_decisions

        test_alerts = [alert for alert in alerts if boundary <= alert.timestamp < stop]
        items = items + alert_items(test_alerts, baseline_decisions(test_alerts, tuned))
    shown = build_queue(items, tuned)
    test_faults = [fault for fault in faults if boundary <= _utc(fault.start) < stop]
    n_days = n_days_between(boundary, stop, tuned)
    scored = score_queue("queue", shown, test_faults, n_days, tuned)
    return caught_fault_ids(shown, test_faults), scored


def _baseline_ids(
    spans: pd.DataFrame,
    faults: list[Fault],
    graphs: dict[str, CallGraph] | None = None,
) -> set[str]:
    caught: set[str] = set()
    for fault in faults:
        if graphs is not None and fault.fault_id in graphs:
            graph = graphs[fault.fault_id]
        else:
            graph = build_graph(spans, fault.start, fault.end)
        if root_cause_hit(baseline_service(graph), fault.component):
            caught.add(fault.fault_id)
    return caught


def evaluate_traces(
    spans: pd.DataFrame,
    metrics: pd.DataFrame,
    faults: list[Fault],
    alerts: list[Alert] | None = None,
    *,
    config: Config | None = None,
    spec: TraceSpec | None = None,
    dataset: str = "synthetic",
    train_end: pd.Timestamp | None = None,
    graphs: dict[str, CallGraph] | None = None,
) -> dict:
    """Train on faults that start before the split. Score the later faults.

    ``graphs`` are one window per fault, already built from that fault's own
    spans. Fit, the baseline, and prediction reuse them and do not walk the
    span table again.
    """
    config = config or Config()
    spec = spec or TraceSpec()
    alerts = alerts or []
    if graphs is None:
        frame = spans.copy()
        if "time" not in frame.columns or not str(frame["time"].dtype).startswith("datetime"):
            from .trace_graph import normalize_spans

            frame = normalize_spans(frame)
    else:
        frame = spans if isinstance(spans, pd.DataFrame) else pd.DataFrame()
    if train_end is not None:
        boundary = _utc(train_end)
    elif not frame.empty and "time" in frame.columns:
        boundary = _utc(frame["time"].min()) + pd.Timedelta(days=config.split_day)
    else:
        boundary = ORIGIN + pd.Timedelta(days=config.split_day)
    test_faults = [fault for fault in faults if boundary <= _utc(fault.start)]
    stop = max((_utc(fault.end) for fault in test_faults), default=boundary + pd.Timedelta(days=1))
    stop = stop + pd.Timedelta(seconds=1)
    archive, status = fit_trace_model(
        frame,
        faults,
        boundary,
        hidden=spec.hidden,
        epochs=spec.epochs,
        seed=spec.seed,
        device=spec.device,
        backend=spec.backend,
        graphs=graphs,
    )
    model_ids: set[str] | None = None
    if archive is not None:
        predicted = predict_services(archive, frame, test_faults, not_before=boundary, graphs=graphs)
        model_ids = caught_ids(predicted, test_faults)
        status = "trained"
    baseline_ids = _baseline_ids(frame, test_faults, graphs)
    residual_ids: set[str] | None = None
    residual_status = status
    residual_archive = None
    if status != "torch-missing":
        extra = _baseline_feature_graphs(frame, faults, graphs, boundary)
        residual_archive, residual_status = fit_trace_model(
            frame,
            faults,
            boundary,
            hidden=spec.hidden,
            epochs=spec.epochs,
            seed=spec.seed,
            device=spec.device,
            backend=spec.backend,
            graphs=extra,
        )
        if residual_archive is not None:
            predicted = predict_services(residual_archive, frame, test_faults, not_before=boundary, graphs=extra)
            residual_ids = caught_ids(predicted, test_faults)
            residual_status = "trained"
    by_k: dict[str, dict] = {}
    catch_ids: dict[str, dict[str, list[str] | None]] = {}
    for budget in spec.budget_ks:
        cfg = replace(config, budget_k=budget)
        if metrics.empty:
            queue_ids: set[str] = set()
            queue_row = {
                "faults_caught": 0,
                "faults": len(test_faults),
                "false_items": None,
                "n_days": None,
                "false_items_per_day": None,
            }
        else:
            queue_ids, queue_row = _queue_scored(metrics, alerts, faults, cfg, boundary, stop)
        entry: dict = {
            "queue": {
                "faults_caught": queue_row["faults_caught"],
                "faults": queue_row["faults"],
                "false_items": queue_row.get("false_items"),
                "n_days": queue_row.get("n_days"),
                "false_items_per_day": queue_row["false_items_per_day"],
                "decision": "baseline",
            },
            "trace_baseline": _decision(queue_ids, baseline_ids, test_faults, spec.seed),
            "trace_model": _variant_row(queue_ids, baseline_ids, model_ids, test_faults, spec.seed, status),
            "trace_residual": _variant_row(
                queue_ids, baseline_ids, residual_ids, test_faults, spec.seed, residual_status
            ),
        }
        by_k[str(budget)] = entry
        catch_ids[str(budget)] = {
            "fault_ids": [fault.fault_id for fault in test_faults],
            "queue": sorted(queue_ids),
            "trace_baseline": sorted(baseline_ids),
            "trace_model": None if model_ids is None else sorted(model_ids),
            "trace_residual": None if residual_ids is None else sorted(residual_ids),
        }
    headline = by_k[str(spec.headline_k)]
    train_cases = sum(1 for fault in faults if _utc(fault.start) < boundary)
    if dataset == "synthetic":
        split = "first week train, later days test"
    elif dataset == "rcaeval":
        split = "first week of each suite train, later days of that suite test"
    else:
        split = "first half of cases train, later half test"
    return {
        "dataset": dataset,
        "gaia": "pending: this run did not score GAIA",
        "rcaeval": "pending: this run did not score RCAEval" if dataset != "rcaeval" else "scored",
        "split": split,
        "headline_k": spec.headline_k,
        "seed": spec.seed,
        "pages": False,
        "flags_changed": False,
        "trace_model": _model_block(archive, status),
        "trace_residual": {
            **_model_block(residual_archive, residual_status),
            "features": "five node features plus max(error_jump, latency_jump)",
        },
        "headline": headline,
        "by_k": by_k,
        "catch_ids": catch_ids,
        "train_cases": train_cases,
        "test_cases": len(test_faults),
        "tiny_train": tiny_train(train_cases),
        "test_faults": len(test_faults),
        "false_items_note": "the trace model nominates one service and adds no queue item",
    }


def _baseline_feature_graphs(
    spans: pd.DataFrame,
    faults: list[Fault],
    graphs: dict[str, CallGraph] | None,
    boundary: pd.Timestamp,
) -> dict[str, CallGraph]:
    """Same windows as the five-feature model, plus the baseline score column."""
    limit = _utc(boundary)
    extra: dict[str, CallGraph] = {}
    for fault in faults:
        if graphs is not None and fault.fault_id in graphs:
            graph = graphs[fault.fault_id]
        else:
            end = _window_end(fault, limit) if _utc(fault.start) < limit else _utc(fault.end)
            if end <= _utc(fault.start):
                continue
            graph = build_graph(spans, fault.start, end)
        extra[fault.fault_id] = with_baseline_feature(graph)
    return extra


def _variant_row(
    queue_ids: set[str],
    baseline_ids: set[str],
    model_ids: set[str] | None,
    faults: list[Fault],
    seed: int,
    status: str,
) -> dict:
    if model_ids is None:
        return {
            "status": status,
            "decision": "skipped",
            "reason": model_skip_reason(status),
            "per_fault_catch_delta": None,
        }
    return _model_decision(queue_ids, baseline_ids, model_ids, faults, seed)


def _model_block(archive: TraceArchive | None, status: str) -> dict:
    block = {
        "status": status,
        "backend": None if archive is None else archive.backend,
        "device": None if archive is None else archive.device,
        "fell_back": None if archive is None else archive.fell_back,
        "gnn_fell_back": None if archive is None else archive.gnn_fell_back,
        "pages": False,
    }
    if archive is None:
        block["reason"] = model_skip_reason(status)
        block["skipped"] = True
    return block


def pooled_false_per_day(suite_reports: dict[str, dict], budget: str) -> float | None:
    """False items per day across suites. Each suite keeps its own day count.

    The rate is the sum of false items divided by the sum of scored days.
    K is still applied inside each suite.
    """
    items = 0
    days = 0
    for report in suite_reports.values():
        row = report["by_k"][budget]["queue"]
        false_items = row.get("false_items")
        n_days = row.get("n_days")
        if false_items is None or n_days is None or n_days <= 0:
            return None
        items += int(false_items)
        days += int(n_days)
    if days <= 0:
        return None
    return round(items / days, 2)


def combine_suite_scores(
    suite_reports: dict[str, dict],
    test_faults: list[Fault],
    *,
    train_cases: int,
    model_ids: set[str] | None,
    model_status: str,
    model_archive: TraceArchive | None,
    residual_ids: set[str] | None,
    residual_status: str,
    residual_archive: TraceArchive | None,
    spec: TraceSpec,
) -> dict:
    """Pooled scores. The queue is the union of the per-suite queues.

    K is not applied again across suites. ``model_ids`` come from one fit on
    every training case. The keep rule is the same one used for a single suite.
    """
    by_k: dict[str, dict] = {}
    for budget in spec.budget_ks:
        key = str(budget)
        queue_ids: set[str] = set()
        baseline_ids: set[str] = set()
        for report in suite_reports.values():
            caught = report["catch_ids"][key]
            queue_ids.update(caught["queue"])
            baseline_ids.update(caught["trace_baseline"])
        test_ids = {fault.fault_id for fault in test_faults}
        queue_ids &= test_ids
        baseline_ids &= test_ids
        by_k[key] = {
            "queue": {
                "faults_caught": len(queue_ids),
                "faults": len(test_faults),
                "false_items_per_day": pooled_false_per_day(suite_reports, key),
                "decision": "baseline",
            },
            "trace_baseline": _decision(queue_ids, baseline_ids, test_faults, spec.seed),
            "trace_model": _variant_row(queue_ids, baseline_ids, model_ids, test_faults, spec.seed, model_status),
            "trace_residual": _variant_row(
                queue_ids, baseline_ids, residual_ids, test_faults, spec.seed, residual_status
            ),
        }
    return {
        "headline_k": spec.headline_k,
        "headline": by_k[str(spec.headline_k)],
        "by_k": by_k,
        "train_cases": train_cases,
        "test_cases": len(test_faults),
        "tiny_train": tiny_train(train_cases),
        "trace_model": _model_block(model_archive, model_status),
        "trace_residual": {
            **_model_block(residual_archive, residual_status),
            "features": "five node features plus max(error_jump, latency_jump)",
        },
        "false_items_note": (
            "pooled queue catches are the union of the per-suite queues; "
            "false items per day is the sum of false items divided by the sum of suite days"
        ),
    }


def _interval(values: list[float], seed: int) -> dict[str, float | int]:
    interval = bootstrap_mean_ci(values, seed=seed)
    return {
        "mean": round(interval.mean, 4),
        "low": round(interval.low, 4),
        "high": round(interval.high, 4),
        "n": interval.n,
    }


def seed_level_decision(catches: list[dict], *, variant: str, seed: int = 42) -> dict:
    """Keep only on a clear majority of seeds and a positive mean-delta interval.

    A clear majority is strictly more than half of the seeds that were scored.
    A skipped seed is not in that count. For each fault, the delta is the
    mean across those seeds of (model caught it) minus (the comparison caught
    it). The 95% interval of that mean, bootstrap seed 42, must lie above 0
    against both the queue and the trace baseline. One scored seed is a
    majority of one, so a single seed keeps only when its own interval does.
    """
    scored = [
        row
        for row in catches
        if row.get("decisions", {}).get(variant) != "skipped" and row.get(variant) is not None
    ]
    skipped = len(catches) - len(scored)
    if not scored:
        return {
            "decision": "skipped",
            "reason": "no scored seed",
            "seeds_kept": 0,
            "seeds_scored": 0,
            "seeds_skipped": skipped,
            "majority": False,
            "against_queue": None,
            "against_baseline": None,
            "faults_caught_by_seed": [],
            "pages": False,
        }
    keeps = sum(1 for row in scored if row["decisions"][variant] == "keep")
    majority = keeps * 2 > len(scored)
    fault_ids = list(scored[0]["fault_ids"])
    against_queue: list[float] = []
    against_baseline: list[float] = []
    for fault_id in fault_ids:
        queue_delta: list[float] = []
        baseline_delta: list[float] = []
        for row in scored:
            hit = float(fault_id in row[variant])
            queue_delta.append(hit - float(fault_id in row["queue"]))
            baseline_delta.append(hit - float(fault_id in row["trace_baseline"]))
        against_queue.append(sum(queue_delta) / len(queue_delta))
        against_baseline.append(sum(baseline_delta) / len(baseline_delta))
    if not fault_ids:
        return {
            "decision": "skipped",
            "reason": "no held-out faults",
            "seeds_kept": keeps,
            "seeds_scored": len(scored),
            "seeds_skipped": skipped,
            "majority": majority,
            "against_queue": None,
            "against_baseline": None,
            "faults_caught_by_seed": [len(row[variant]) for row in scored],
            "pages": False,
        }
    queue_ci = _interval(against_queue, seed)
    baseline_ci = _interval(against_baseline, seed)
    intervals_ok = float(queue_ci["low"]) > 0.0 and float(baseline_ci["low"]) > 0.0
    keep = majority and intervals_ok
    if keep:
        reason = (
            "kept on a clear majority of seeds, and the mean per-fault catch "
            "difference stays above 0 against both the queue and the trace baseline"
        )
    elif not majority:
        reason = "not a clear majority of scored seeds"
    else:
        reason = "the mean per-fault catch difference does not stay above 0"
    return {
        "decision": "keep" if keep else "drop",
        "reason": reason,
        "seeds_kept": keeps,
        "seeds_scored": len(scored),
        "seeds_skipped": skipped,
        "majority": majority,
        "against_queue": queue_ci,
        "against_baseline": baseline_ci,
        "faults_caught_by_seed": [len(row[variant]) for row in scored],
        "pages": False,
    }


def _model_decision(
    queue_ids: set[str],
    baseline_ids: set[str],
    model_ids: set[str],
    faults: list[Fault],
    seed: int,
) -> dict:
    against_queue = _decision(queue_ids, model_ids, faults, seed)
    against_baseline = _decision(baseline_ids, model_ids, faults, seed)
    keep = against_queue["decision"] == "keep" and against_baseline["decision"] == "keep"
    return {
        "status": "scored",
        "decision": "keep" if keep else "drop",
        "reason": (
            "the 95% interval lies above zero against both the trace baseline and the queue"
            if keep
            else "the 95% interval does not lie above zero against both the trace baseline and the queue"
        ),
        "faults_caught": len(model_ids),
        "faults": len(faults),
        "against_queue": against_queue,
        "against_baseline": against_baseline,
    }


def synthetic_traces(seed: int = 42, days: int = 14) -> tuple[pd.DataFrame, pd.DataFrame, list[Fault]]:
    """Three services. The root cause is ``db``.

    An ``error`` window raises db's error rate. A ``decoy`` window also makes
    the caller ``api`` much slower, so the latency jump points at the caller
    while the error jump stays on ``db``. Two test ``error`` windows also move
    db's memory, which is what the jump queue can see.
    """
    rng = np.random.default_rng(seed)
    plan = [
        (1, 2, "error", False),
        (1, 14, "decoy", False),
        (2, 2, "error", False),
        (2, 14, "decoy", False),
        (3, 2, "error", False),
        (3, 14, "decoy", False),
        (4, 2, "error", False),
        (4, 14, "decoy", False),
        (8, 2, "error", True),
        (8, 14, "decoy", False),
        (9, 2, "error", True),
        (9, 14, "decoy", False),
        (10, 2, "error", False),
        (10, 14, "decoy", False),
        (11, 2, "error", False),
        (11, 14, "decoy", False),
    ]
    faults: list[Fault] = []
    windows: list[tuple[pd.Timestamp, pd.Timestamp, str]] = []
    jumps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for day, hour, kind, memory in plan:
        start = ORIGIN + pd.Timedelta(days=day, hours=hour)
        end = start + pd.Timedelta(minutes=40)
        faults.append(Fault(f"f-{day}-{hour}", start, end, "db", kind))
        windows.append((start, end, kind))
        if memory:
            jumps.append((start, start + pd.Timedelta(minutes=20)))

    stamps = pd.date_range(ORIGIN, periods=days * 24 * 12, freq="5min")
    rows: list[dict[str, object]] = []
    for stamp_at, stamp in enumerate(stamps):
        window_kind = next((item[2] for item in windows if item[0] <= stamp < item[1]), None)
        trace = f"t{stamp_at}"
        web = max(1.0, 30.0 + float(rng.normal(0, 0.5)))
        api = 20.0 + float(rng.normal(0, 0.5))
        db = 15.0 + float(rng.normal(0, 0.5))
        api_error = 0
        db_error = 0
        if window_kind == "error":
            db = 90.0 + float(rng.normal(0, 1.0))
            db_error = 1
        elif window_kind == "decoy":
            api = 160.0 + float(rng.normal(0, 1.0))
            db = 18.0 + float(rng.normal(0, 0.5))
            db_error = 1
        rows.append(_span(stamp, trace, "w", "", "web", web, 0))
        rows.append(_span(stamp, trace, "a", "w", "api", max(1.0, api), api_error))
        rows.append(_span(stamp, trace, "d", "a", "db", max(1.0, db), db_error))
    spans = pd.DataFrame(rows)
    spans["time"] = pd.to_datetime(spans["time"], utc=True)

    metric_stamps = pd.date_range(ORIGIN, periods=days * 24 * 60, freq="1min")
    base = np.full(len(metric_stamps), 2.0e9)
    hot = base.copy()
    for start, end in jumps:
        hot[(metric_stamps >= start) & (metric_stamps < end)] = 3.2e9
    frames = []
    for service, values in (("web", base), ("api", base), ("db", hot)):
        frames.append(
            pd.DataFrame(
                {
                    "timestamp": metric_stamps,
                    "value": values,
                    "service": service,
                    "series_id": service,
                }
            )
        )
    return spans, pd.concat(frames, ignore_index=True), faults


def _span(
    stamp: pd.Timestamp,
    trace: str,
    span: str,
    parent: str,
    service: str,
    duration: float,
    error: int,
) -> dict[str, object]:
    return {
        "time": stamp,
        "trace_id": trace,
        "span_id": f"{trace}-{span}",
        "parent_span_id": f"{trace}-{parent}" if parent else "",
        "service": service,
        "duration": duration,
        "error": error,
    }
