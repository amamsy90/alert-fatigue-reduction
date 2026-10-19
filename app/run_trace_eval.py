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
from pathlib import Path

import pandas as pd

from src.config import Config
from src.entities import Alert, Fault
from src.trace_eval import TraceSpec, evaluate_traces, synthetic_traces
from src.trace_graph import CallGraph, build_graph, normalize_spans
from src.trace_model import _utc, _window_end
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


def _case_graph(case, end: pd.Timestamp) -> tuple[CallGraph | None, int]:
    """Build this recording's window and drop the span table."""
    from run_rcaeval_logs import ROOT as CASE_ROOT

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
) -> tuple[dict[str, CallGraph], pd.DataFrame, list[Fault], list[Alert], Config, pd.Timestamp, list[str]]:
    """One graph per recording. Span tables are not concatenated.

    Each RCAEval case is its own experiment. The graph uses that case's spans
    only, and the same graph is what fit, the baseline, and prediction score.
    """
    from run_rcaeval_logs import base_config, case_alerts, case_metrics, load_cases

    config = base_config()
    planned = []
    skipped: list[str] = []
    suites = (suite,) if suite else ("RE2", "RE3")
    for one_suite in suites:
        for system in ("ob", "ss", "tt"):
            if system not in _TRACED:
                skipped.append(f"{one_suite}-{system}: no traces in RCAEval")
                continue
            planned.extend(load_cases(one_suite, system))
    planned = limit_cases(planned, max_cases)
    if not planned:
        raise SystemExit(
            "No RCAEval trace cases were selected. Nothing was scored. "
            "Sock Shop has no traces. Pass --suite RE2 or RE3."
        )
    layout_origin = min(case.at(case.start) for case in planned)
    boundary = layout_origin + pd.Timedelta(days=config.split_day)
    graphs: dict[str, CallGraph] = {}
    metrics = []
    alerts: list[Alert] = []
    faults: list[Fault] = []
    started = time.perf_counter()
    print(
        f"rcaeval cases={len(planned)} suite={suite or 'RE2+RE3'} "
        f"boundary={boundary.isoformat()} rss_gb={_rss_gb():.2f}",
        flush=True,
    )
    for index, case in enumerate(planned, start=1):
        from run_rcaeval_logs import ROOT as CASE_ROOT

        path = CASE_ROOT / case.case / "traces.parquet"
        if not path.is_file():
            skipped.append(case.case)
            print(f"trace case {index}/{len(planned)} {case.case} missing traces.parquet", flush=True)
            continue
        fault = case.as_fault()
        end = _window_end(fault, boundary) if _utc(fault.start) < boundary else _utc(fault.end)
        print(f"trace case {index}/{len(planned)} {case.case} loading", flush=True)
        try:
            graph, n_spans = _case_graph(case, end)
        except (OSError, ValueError, KeyError) as exc:
            skipped.append(case.case)
            print(f"trace case {index}/{len(planned)} {case.case} skipped: {exc}", flush=True)
            continue
        if graph is None:
            skipped.append(case.case)
            print(f"trace case {index}/{len(planned)} {case.case} no spans in the window", flush=True)
            continue
        graphs[fault.fault_id] = graph
        faults.append(fault)
        metrics.append(case_metrics(case))
        alerts.extend(case_alerts(case))
        role = "train" if _utc(fault.start) < boundary else "test"
        print(
            f"trace case {index}/{len(planned)} {case.case} spans={n_spans} "
            f"services={len(graph.services)} split={role} "
            f"elapsed_s={time.perf_counter() - started:.1f} rss_gb={_rss_gb():.2f}",
            flush=True,
        )
    if not graphs:
        raise SystemExit(
            "No RCAEval traces.parquet files are in data/rcaeval_re23. Nothing was scored. "
            "On the laptop, from the repo root: python tools/fetch_rcaeval_traces.py --suite RE2 "
            "and python tools/fetch_rcaeval_traces.py --suite RE3"
        )
    # A training window that crosses the split was clipped above. Reload is not
    # required: _window_end was applied before the graph was built.
    print(
        f"rcaeval graphs={len(graphs)} skipped={len(skipped)} "
        f"elapsed_s={time.perf_counter() - started:.1f} rss_gb={_rss_gb():.2f}",
        flush=True,
    )
    return graphs, pd.concat(metrics, ignore_index=True), faults, alerts, config, boundary, skipped


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
        graphs, metrics, faults, alerts, config, boundary, skipped = _load_rcaeval(args.suite, args.max_cases)
        spans = pd.DataFrame()
        dataset = "rcaeval"
    else:
        spans, metrics, faults = synthetic_traces(seed=args.seed)
        alerts = []
        config = Config()
        graphs = None
        boundary = None
        dataset = "synthetic"
    spec = TraceSpec(
        epochs=args.epochs,
        device=args.device,
        backend=args.backend,
        seed=args.seed,
        hidden=16 if dataset == "rcaeval" else 8,
    )
    print("scoring the trace model, the jump baseline, and the queue", flush=True)
    report = evaluate_traces(
        spans,
        metrics,
        faults,
        alerts,
        config=config,
        spec=spec,
        dataset=dataset,
        train_end=boundary,
        graphs=graphs,
    )
    if dataset == "rcaeval":
        report["cases"] = len(faults)
        report["max_cases"] = args.max_cases
    report["traces_skipped"] = skipped
    if dataset == "synthetic":
        report["rcaeval"] = "pending: this run did not score RCAEval"
    out = Path(args.out) if args.out else Path("results") / f"trace_eval_{dataset}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    headline = report["headline"]
    print(json.dumps({"dataset": dataset, "out": str(out), "gaia": report["gaia"], "rcaeval": report["rcaeval"]}))
    for name in ("queue", "trace_baseline", "trace_model"):
        row = headline[name]
        print(
            f"K={report['headline_k']} {name:16s} decision={row.get('decision')} "
            f"caught={row.get('faults_caught')} delta={row.get('per_fault_catch_delta')}"
        )
    record_run(
        f"trace_eval_{dataset}",
        config=config,
        outputs=[out],
        metrics={"dataset": dataset, "status": report["trace_model"]["status"], "headline_k": report["headline_k"]},
        note=report["rcaeval"],
    )


if __name__ == "__main__":
    main()
