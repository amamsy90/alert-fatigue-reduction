"""Compare the trace model with the jump baseline and the current queue.

Keep the model only when the 95% bootstrap interval on the per-fault catch
difference lies entirely above zero against both, at K = 20. A catch counts
only on the root-cause service. The flag stays off. GAIA is not a trace
dataset here. RCAEval is scored only when ``traces.parquet`` is on disk.

    python run_trace_eval.py --data synthetic --backend plain --epochs 20
    python tools/fetch_rcaeval_re23.py
    python tools/fetch_rcaeval_traces.py --suite RE2
    python run_trace_eval.py --data rcaeval --device cuda --backend auto --epochs 8
    python run_trace_eval.py --data rcaeval --suite RE2 --max-cases 2
"""
from __future__ import annotations

import argparse
import json
import resource
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

from run_rcaeval_logs import ROOT as CASE_ROOT
from run_rcaeval_logs import SYSTEMS, Case, base_config, case_alerts, case_metrics, load_cases
from src.config import Config
from src.entities import Alert, Fault
from src.trace_eval import (
    TraceSpec,
    combine_suite_scores,
    evaluate_traces,
    suite_day,
    synthetic_traces,
    tiny_train,
)
from src.trace_graph import CallGraph, build_graph, normalize_spans, with_baseline_feature
from src.trace_model import TraceArchive, _utc, caught_ids, fit_trace_model, predict_services
from src.tracking import record_run

ROOT = Path("data/rcaeval_re23")
# Sock Shop recordings have no traces in RCAEval. Online Boutique and Train Ticket do.
_TRACED = {"ob", "tt"}


def cases_missing_message(root: Path | None = None) -> str | None:
    """``cases.parquet`` comes from ``tools/fetch_rcaeval_re23.py``, not the trace fetch."""
    folder = root if root is not None else ROOT
    if (folder / "cases.parquet").is_file():
        return None
    return (
        "RCAEval cases.parquet is not in data/rcaeval_re23. Nothing was scored. "
        "From the repo root, run python tools/fetch_rcaeval_re23.py first "
        "(it writes cases.parquet), then python tools/fetch_rcaeval_traces.py, "
        "then from app/ python run_trace_eval.py --data rcaeval."
    )


def _traces_present() -> bool:
    if not ROOT.exists():
        return False
    return any(ROOT.glob("*/traces.parquet"))


def limit_cases(cases: list, max_cases: int | None) -> list:
    """The first ``max_cases`` recordings. Zero or None keeps every case."""
    if max_cases is None or max_cases <= 0:
        return list(cases)
    return list(cases[:max_cases])


def wanted_span_columns(names: list[str]) -> list[str]:
    """Columns the graph needs. Tags and logs stay on disk."""
    keys = {
        "traceid",
        "spanid",
        "parentspanid",
        "parentspan",
        "servicename",
        "service",
        "duration",
        "durationms",
        "latency",
        "statuscode",
        "status",
        "responsecode",
        "starttimemillis",
        "starttimeunixnano",
        "starttimeunixmicro",
        "starttime",
        "timestamp",
        "time",
    }
    chosen: list[str] = []
    for name in names:
        raw = str(name).strip().lower()
        key = raw.replace("_", "")
        if key in keys or "time" in raw or "timestamp" in raw:
            chosen.append(str(name))
    return chosen


def _read_spans(path: Path) -> pd.DataFrame:
    """One recording, projected to the graph columns. The file is not kept open."""
    try:
        import pyarrow.parquet as pq

        names = list(pq.ParquetFile(path).schema_arrow.names)
        columns = wanted_span_columns(names)
        if columns:
            return pd.read_parquet(path, columns=columns)
    except ImportError:
        pass
    return pd.read_parquet(path)


def _rss_gb() -> float:
    # Linux reports ru_maxrss in kilobytes.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def spread_case(case: Case, suite: str) -> tuple[Case, int]:
    """Move one recording onto that suite's day block. The local day is kept.

    RE2 and RE3 both start at 2024-01-01 01:00 UTC when ``day`` restarts at 0,
    and series ids such as ``adservice_cpu`` are not case-scoped. The same
    ``(series_id, timestamp)`` then appears twice and ``detect_jumps`` raises.
    RE3 is placed ``SUITE_DAY_BLOCK`` days later. Online Boutique and Train
    Ticket keep the same local day, so they still share a clock inside a suite.
    """
    local_day = case.day
    return replace(case, day=suite_day(local_day, suite)), local_day


@dataclass
class Recording:
    suite: str
    system: str
    local_day: int
    fault: Fault
    metrics: pd.DataFrame
    alerts: list[Alert]
    graph: CallGraph


def _case_graph(case: Case, end: pd.Timestamp) -> tuple[CallGraph | None, int]:
    """Build this recording's window and drop the span table."""
    path = CASE_ROOT / case.case / "traces.parquet"
    raw = _read_spans(path)
    frame = normalize_spans(raw)
    del raw
    n_spans = int(len(frame))
    if frame.empty:
        return None, 0
    origin = pd.Timestamp(case.start, unit="s", tz="UTC")
    frame["time"] = frame["time"] + (case.at(case.start) - origin)
    start = _utc(case.as_fault().start)
    stop = _utc(end)
    if stop <= start:
        return None, n_spans
    frame = frame.loc[frame["time"] < stop]
    graph = build_graph(frame, start, stop)
    del frame
    return graph, n_spans


def _load_rcaeval(
    suite: str | None,
    max_cases: int | None,
) -> tuple[list[Recording], Config, list[str]]:
    """One graph per recording. Span tables are not concatenated.

    Each RCAEval case is its own experiment. The graph uses that case's spans
    only. Suites are laid out on non-overlapping days before metrics, alerts,
    faults, and the graph are built, so a combined RE2+RE3 frame does not
    repeat ``(series_id, timestamp)``.
    """
    config = base_config()
    selected: list[tuple[str, str, Case]] = []
    skipped: list[str] = []
    suites = (suite,) if suite else ("RE2", "RE3")
    for one_suite in suites:
        for system in ("ob", "ss", "tt"):
            if system not in _TRACED:
                skipped.append(f"{one_suite}-{system}: no traces in RCAEval")
                continue
            selected.extend((one_suite, system, case) for case in load_cases(one_suite, system))
    selected = limit_cases(selected, max_cases)
    if not selected:
        raise SystemExit(
            "No RCAEval trace cases were selected. Nothing was scored. "
            "Sock Shop has no traces. Pass --suite RE2 or RE3."
        )
    recordings: list[Recording] = []
    started = time.perf_counter()
    print(
        f"rcaeval cases={len(selected)} suite={suite or 'RE2+RE3'} "
        f"layout=suite-day-block rss_gb={_rss_gb():.2f}",
        flush=True,
    )
    for index, (one_suite, system, case) in enumerate(selected, start=1):
        placed, local_day = spread_case(case, one_suite)
        path = CASE_ROOT / placed.case / "traces.parquet"
        if not path.is_file():
            skipped.append(placed.case)
            print(f"trace case {index}/{len(selected)} {placed.case} missing traces.parquet", flush=True)
            continue
        fault = placed.as_fault()
        end = _utc(fault.end)
        print(f"trace case {index}/{len(selected)} {placed.case} loading", flush=True)
        try:
            graph, n_spans = _case_graph(placed, end)
        except (OSError, ValueError, KeyError) as exc:
            skipped.append(placed.case)
            print(f"trace case {index}/{len(selected)} {placed.case} skipped: {exc}", flush=True)
            continue
        if graph is None:
            skipped.append(placed.case)
            print(f"trace case {index}/{len(selected)} {placed.case} no spans in the window", flush=True)
            continue
        recordings.append(
            Recording(
                one_suite,
                system,
                local_day,
                fault,
                case_metrics(placed),
                case_alerts(placed),
                graph,
            )
        )
        role = "train" if local_day < config.split_day else "test"
        print(
            f"trace case {index}/{len(selected)} {placed.case} spans={n_spans} "
            f"services={len(graph.services)} split={role} local_day={local_day} "
            f"elapsed_s={time.perf_counter() - started:.1f} rss_gb={_rss_gb():.2f}",
            flush=True,
        )
    if not recordings:
        raise SystemExit(
            "No RCAEval traces.parquet files are in data/rcaeval_re23. Nothing was scored. "
            "On the laptop, from the repo root: python tools/fetch_rcaeval_traces.py --suite RE2 "
            "and python tools/fetch_rcaeval_traces.py --suite RE3"
        )
    print(
        f"rcaeval graphs={len(recordings)} skipped={len(skipped)} "
        f"elapsed_s={time.perf_counter() - started:.1f} rss_gb={_rss_gb():.2f}",
        flush=True,
    )
    return recordings, config, skipped


def _concat_metrics(rows: list[Recording]) -> pd.DataFrame:
    frames = [row.metrics for row in rows if not row.metrics.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _split_boundary(rows: list[Recording], split_day: int) -> pd.Timestamp:
    """First test-case start. Training is every local day before ``split_day``."""
    test = [row for row in rows if row.local_day >= split_day]
    if test:
        return min(_utc(row.fault.start) for row in test)
    train = [row for row in rows if row.local_day < split_day]
    if train:
        return max(_utc(row.fault.end) for row in train) + pd.Timedelta(seconds=1)
    return _utc(rows[0].fault.start)


def _public(report: dict) -> dict:
    """Drop catch id lists. The scores stay."""
    cleaned = dict(report)
    cleaned.pop("catch_ids", None)
    return cleaned


def _score_slice(rows: list[Recording], *, spec: TraceSpec, config: Config) -> dict:
    boundary = _split_boundary(rows, config.split_day)
    report = evaluate_traces(
        pd.DataFrame(),
        _concat_metrics(rows),
        [row.fault for row in rows],
        [alert for row in rows for alert in row.alerts],
        config=config,
        spec=spec,
        dataset="rcaeval",
        train_end=boundary,
        graphs={row.fault.fault_id: row.graph for row in rows},
    )
    train_cases = sum(1 for row in rows if row.local_day < config.split_day)
    test_cases = sum(1 for row in rows if row.local_day >= config.split_day)
    report["train_cases"] = train_cases
    report["test_cases"] = test_cases
    report["tiny_train"] = tiny_train(train_cases)
    return report


def _fit_variant(
    train: list[Recording],
    test: list[Recording],
    spec: TraceSpec,
    *,
    residual: bool,
) -> tuple[TraceArchive | None, str, set[str]]:
    """One fit on every training recording. Test graphs are not training rows."""
    graphs: dict[str, CallGraph] = {}
    for row in train + test:
        graph = with_baseline_feature(row.graph) if residual else row.graph
        graphs[row.fault.fault_id] = graph
    faults = [row.fault for row in train]
    if not faults:
        return None, "too-few-windows", set()
    end = max(_utc(row.fault.end) for row in train) + pd.Timedelta(seconds=1)
    archive, status = fit_trace_model(
        pd.DataFrame(),
        faults,
        end,
        hidden=spec.hidden,
        epochs=spec.epochs,
        seed=spec.seed,
        device=spec.device,
        backend=spec.backend,
        graphs=graphs,
    )
    if archive is None:
        return None, status, set()
    test_faults = [row.fault for row in test]
    predicted = predict_services(archive, pd.DataFrame(), test_faults, graphs=graphs)
    return archive, "trained", caught_ids(predicted, test_faults)


def score_recordings(rows: list[Recording], *, spec: TraceSpec, config: Config) -> dict:
    """Per suite, per system, and pooled. Each slice records train and test counts."""
    by_suite: dict[str, dict] = {}
    by_system: dict[str, dict] = {}
    for suite in ("RE2", "RE3"):
        suite_rows = [row for row in rows if row.suite == suite]
        if not suite_rows:
            continue
        by_suite[suite] = _score_slice(suite_rows, spec=spec, config=config)
        by_suite[suite]["queue_scope"] = "Online Boutique and Train Ticket share K"
        for system in ("ob", "tt"):
            system_rows = [row for row in suite_rows if row.system == system]
            if not system_rows:
                continue
            named = f"{suite}/{SYSTEMS[system]}"
            by_system[named] = _score_slice(system_rows, spec=spec, config=config)
            by_system[named]["queue_scope"] = "this system only; K is not shared with the other system"
    train_rows = [row for row in rows if row.local_day < config.split_day]
    test_rows = [row for row in rows if row.local_day >= config.split_day]
    archive, model_status, model_ids = _fit_variant(train_rows, test_rows, spec, residual=False)
    if model_status == "torch-missing":
        residual_archive, residual_status, residual_ids = None, "torch-missing", set()
    else:
        residual_archive, residual_status, residual_ids = _fit_variant(train_rows, test_rows, spec, residual=True)
    pooled = combine_suite_scores(
        by_suite,
        [row.fault for row in test_rows],
        train_cases=len(train_rows),
        model_ids=None if archive is None else model_ids,
        model_status=model_status,
        model_archive=archive,
        residual_ids=None if residual_archive is None else residual_ids,
        residual_status=residual_status,
        residual_archive=residual_archive,
        spec=spec,
    )
    pooled["dataset"] = "rcaeval"
    pooled["gaia"] = "pending: this run did not score GAIA"
    pooled["rcaeval"] = "scored"
    pooled["split"] = "first week of each suite train, later days of that suite test"
    pooled["pages"] = False
    pooled["flags_changed"] = False
    report = {
        "dataset": "rcaeval",
        "gaia": pooled["gaia"],
        "rcaeval": "scored",
        "split": pooled["split"],
        "headline_k": spec.headline_k,
        "pages": False,
        "flags_changed": False,
        "layout": (
            "RE2 keeps the days from load_cases. RE3 is shifted by 1000 days so a "
            "combined run does not repeat (series_id, timestamp). Online Boutique and "
            "Train Ticket still share a day inside a suite. tiny_train is true when "
            "a slice has fewer than 20 training cases."
        ),
        "trace_model": pooled["trace_model"],
        "trace_residual": pooled["trace_residual"],
        "headline": pooled["headline"],
        "by_k": pooled["by_k"],
        "train_cases": pooled["train_cases"],
        "test_cases": pooled["test_cases"],
        "tiny_train": pooled["tiny_train"],
        "pooled": _public(pooled),
        "by_suite": {name: _public(body) for name, body in by_suite.items()},
        "by_system": {name: _public(body) for name, body in by_system.items()},
        "false_items_note": pooled["false_items_note"],
        "test_faults": len(test_rows),
    }
    return report


def _print_block(label: str, report: dict) -> None:
    print(
        f"{label} train={report.get('train_cases')} test={report.get('test_cases')} "
        f"tiny_train={report.get('tiny_train')}",
        flush=True,
    )
    headline = report["headline"]
    for name in ("queue", "trace_baseline", "trace_model", "trace_residual"):
        row = headline.get(name)
        if row is None:
            continue
        print(
            f"  K={report['headline_k']} {name:16s} decision={row.get('decision')} "
            f"caught={row.get('faults_caught')} false/day={row.get('false_items_per_day')} "
            f"delta={row.get('per_fault_catch_delta')}",
            flush=True,
        )
        reason = row.get("reason")
        if row.get("decision") == "skipped" and reason:
            print(f"    {reason}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Time-split eval of the optional trace model.")
    parser.add_argument("--data", choices=("synthetic", "rcaeval"), default="synthetic")
    parser.add_argument("--suite", choices=("RE2", "RE3"), default=None)
    parser.add_argument(
        "--max-cases",
        type=int,
        default=0,
        help="Read only the first N trace cases (0 means every RE2/RE3 case with traces).",
    )
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--backend", default="plain", choices=("auto", "plain", "pyg"))
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    skipped: list[str] = []
    if args.data == "rcaeval":
        missing = cases_missing_message()
        if missing:
            raise SystemExit(missing)
        if not _traces_present():
            raise SystemExit(
                "RCAEval traces are not in this run (data/rcaeval_re23/*/traces.parquet). "
                "Nothing was scored. On the laptop: python tools/fetch_rcaeval_traces.py --suite RE2"
            )
        recordings, config, skipped = _load_rcaeval(args.suite, args.max_cases)
        dataset = "rcaeval"
    else:
        spans, metrics, faults = synthetic_traces(seed=args.seed)
        alerts: list = []
        config = Config()
        recordings = None
        dataset = "synthetic"
    spec = TraceSpec(
        epochs=args.epochs,
        device=args.device,
        backend=args.backend,
        seed=args.seed,
        hidden=16 if dataset == "rcaeval" else 8,
    )
    print("scoring the trace model, the jump baseline, and the queue", flush=True)
    if recordings is not None:
        report = score_recordings(recordings, spec=spec, config=config)
        report["cases"] = len(recordings)
        report["max_cases"] = args.max_cases
    else:
        report = evaluate_traces(
            spans,
            metrics,
            faults,
            alerts,
            config=config,
            spec=spec,
            dataset=dataset,
        )
        report.pop("catch_ids", None)
        report["rcaeval"] = "pending: this run did not score RCAEval"
    report["traces_skipped"] = skipped
    out = Path(args.out) if args.out else Path("results") / f"trace_eval_{dataset}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dataset": dataset, "out": str(out), "gaia": report["gaia"], "rcaeval": report["rcaeval"]}))
    _print_block("pooled" if dataset == "rcaeval" else "synthetic", report)
    for name, body in report.get("by_suite", {}).items():
        _print_block(name, body)
    for name, body in report.get("by_system", {}).items():
        _print_block(name, body)
    record_run(
        f"trace_eval_{dataset}",
        config=config,
        outputs=[out],
        metrics={"dataset": dataset, "status": report["trace_model"]["status"], "headline_k": report["headline_k"]},
        note=report["rcaeval"],
    )


if __name__ == "__main__":
    main()
