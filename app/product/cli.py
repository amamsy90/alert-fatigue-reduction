"""Score your own export from the command line — no server, no labels needed.

    python -m product.cli score --alerts alerts.csv --metrics memory.csv --out queue.csv

alerts.csv:  timestamp, source, severity, message [, alert_id, alert_type]
metrics:     one CSV with timestamp, value, service — or a folder of GAIA-style files
faults.csv:  optional fault_id, start, end, component — enables training and a score

Writes one row per queue item (metric jumps, monitoring gaps, surfaced alerts) in
the order on-call would have seen them, and prints a per-day summary. Settings
come from the same environment variables as the web app (see product/settings.py);
the most useful ones are also flags here.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.entities import SURFACED_ACTIONS
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults

from .pipeline import load_metrics_csv, score_alerts
from .settings import config_from_env


def _queue_rows(result) -> list[dict]:
    rows = []
    for item in result.jumps:
        if not item["shown"]:
            continue
        rows.append({
            "shown_at": item["confirm"], "kind": item["kind"], "service": item["service"],
            "detail": "metrics stopped" if item["kind"] == "gap" else f"+{item['rise']:.4g} at confirmation",
            "until": item["end"], "alerts_inside": item["alerts_inside"],
        })
    for alert, decision in zip(result.alerts, result.decisions):
        if decision.action not in SURFACED_ACTIONS:
            continue
        rows.append({
            "shown_at": alert.timestamp.isoformat(), "kind": f"alert:{decision.action.value}",
            "service": alert.source, "detail": alert.message[:120], "until": "",
            "alerts_inside": "", "reason": decision.reason,
        })
    rows.sort(key=lambda r: r["shown_at"])
    return rows


def score(args: argparse.Namespace) -> int:
    config = config_from_env(args.budget_k)
    overrides = {
        field: getattr(args, flag)
        for flag, field in (("jump_mode", "memory_jump_mode"), ("jump_bytes", "memory_jump_bytes"),
                            ("jump_ratio", "memory_jump_ratio"), ("day_offset_hours", "day_offset_hours"))
        if getattr(args, flag) is not None
    }
    config = replace(config, **overrides)
    alerts = load_alerts(args.alerts) if args.alerts else []
    faults = None
    if args.faults:
        alerts, faults = align_alerts_to_faults(alerts, load_faults(args.faults))
    metrics = load_metrics_csv(args.metrics) if args.metrics else None
    if not alerts and metrics is None:
        print("Give --alerts and/or --metrics", file=sys.stderr)
        return 2
    result = score_alerts(alerts, metrics=metrics, budget_k=args.budget_k, config=config, faults=faults)
    rows = _queue_rows(result)
    out = Path(args.out)
    pd.DataFrame(rows).to_csv(out, index=False)
    frame = pd.DataFrame(rows)
    print(f"Mode: {result.model_used}. {len(alerts)} alerts in, {len(rows)} queue items out -> {out}")
    if not frame.empty:
        frame["day"] = frame["shown_at"].str.slice(0, 10)
        summary = frame.groupby(["day", "kind"]).size().unstack(fill_value=0)
        print(summary.to_string())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m product.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("score", help="score an alerts/metrics export into a queue csv")
    s.add_argument("--alerts", help="alerts csv")
    s.add_argument("--metrics", help="metrics csv (timestamp,value,service) or GAIA metric folder")
    s.add_argument("--faults", help="optional faults csv (labels)")
    s.add_argument("--out", default="queue.csv")
    s.add_argument("--budget-k", type=int, default=20)
    s.add_argument("--jump-mode", choices=("absolute", "relative"))
    s.add_argument("--jump-bytes", type=float, help="absolute mode rise, e.g. 5e8")
    s.add_argument("--jump-ratio", type=float, help="relative mode rise, e.g. 0.3 for +30%%")
    s.add_argument("--day-offset-hours", type=float)
    args = parser.parse_args(argv)
    return score(args)


if __name__ == "__main__":
    raise SystemExit(main())
