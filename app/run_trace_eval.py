"""Compare the trace model with the jump baseline and the current queue.

Keep the model only when the 95% bootstrap interval on the per-fault catch
difference lies entirely above zero against both, at K = 20. A catch counts
only on the root-cause service. The flag stays off. GAIA is not a trace
dataset here. RCAEval is scored only when ``traces.parquet`` is on disk.

    python run_trace_eval.py --data synthetic --backend plain --epochs 20
    python tools/fetch_rcaeval_re23.py
    python tools/fetch_rcaeval_traces.py --suite RE2
    python run_trace_eval.py --data rcaeval --device cuda --backend auto --epochs 8
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from src.config import Config
from src.trace_eval import TraceSpec, evaluate_traces, synthetic_traces
from src.trace_graph import normalize_spans
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


def _load_rcaeval(suite: str | None) -> tuple[pd.DataFrame, pd.DataFrame, list, list, Config]:
    """Lay cases out the way ``run_rcaeval_logs`` does. Missing traces are skipped."""
    from run_rcaeval_logs import ROOT as CASE_ROOT
    from run_rcaeval_logs import base_config, case_alerts, case_metrics, load_cases

    spans = []
    metrics = []
    alerts = []
    faults = []
    skipped = []
    suites = (suite,) if suite else ("RE2", "RE3")
    for one_suite in suites:
        for system in ("ob", "ss", "tt"):
            if system not in _TRACED:
                skipped.append(f"{one_suite}-{system}: no traces in RCAEval")
                continue
            cases = load_cases(one_suite, system)
            for case in cases:
                path = CASE_ROOT / case.case / "traces.parquet"
                if not path.is_file():
                    skipped.append(case.case)
                    continue
                raw = normalize_spans(pd.read_parquet(path))
                if raw.empty:
                    skipped.append(case.case)
                    continue
                origin = pd.Timestamp(case.start, unit="s", tz="UTC")
                raw = raw.copy()
                raw["time"] = raw["time"] + (case.at(case.start) - origin)
                spans.append(raw)
                metrics.append(case_metrics(case))
                alerts.extend(case_alerts(case))
                faults.append(case.as_fault())
    if not spans:
        raise SystemExit(
            "No RCAEval traces.parquet files are in data/rcaeval_re23. Nothing was scored. "
            "On the laptop, from the repo root: python tools/fetch_rcaeval_traces.py --suite RE2 "
            "and python tools/fetch_rcaeval_traces.py --suite RE3"
        )
    return (
        pd.concat(spans, ignore_index=True),
        pd.concat(metrics, ignore_index=True),
        faults,
        alerts,
        base_config(),
    ), skipped  # type: ignore[return-value]


def main() -> None:
    parser = argparse.ArgumentParser(description="Time-split eval of the optional trace model.")
    parser.add_argument("--data", choices=("synthetic", "rcaeval"), default="synthetic")
    parser.add_argument("--suite", choices=("RE2", "RE3"), default=None)
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
        loaded, skipped = _load_rcaeval(args.suite)
        spans, metrics, faults, alerts, config = loaded
        dataset = "rcaeval"
    else:
        spans, metrics, faults = synthetic_traces(seed=args.seed)
        alerts = []
        config = Config()
        dataset = "synthetic"
    spec = TraceSpec(
        epochs=args.epochs,
        device=args.device,
        backend=args.backend,
        seed=args.seed,
        hidden=16 if dataset == "rcaeval" else 8,
    )
    report = evaluate_traces(
        spans, metrics, faults, alerts, config=config, spec=spec, dataset=dataset
    )
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
