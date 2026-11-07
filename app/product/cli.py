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
            "detail": {"gap": "metrics stopped", "leak": f"slow rise +{item['rise']:.4g}"}.get(
                item["kind"], f"+{item['rise']:.4g} at confirmation"
            ),
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
    audit = (getattr(result, "optional_models", None) or {}).get("ranker_audit")
    if audit:
        _attach_ranker_notes(rows, audit)
    return rows


def _attach_ranker_notes(rows: list[dict], audit: dict) -> None:
    """Extra columns only when a ranker audit was produced. Absent otherwise."""
    notes: dict[tuple[str, str, str], str] = {}
    for bucket in ("queue_reasons", "needs_human_review", "queue_disagreements"):
        for item in audit.get(bucket) or []:
            text = item.get("review_reason") or ""
            reasons = item.get("reasons") or []
            parts = [f"{reason['feature']} {reason['contribution']:+.3f}" for reason in reasons[:3]]
            if parts:
                text = (text + "; " if text else "") + ", ".join(parts)
            if not text:
                continue
            key = (str(item.get("shown_at", ""))[:19], str(item.get("kind")), str(item.get("service")))
            notes.setdefault(key, text)
    for row in rows:
        kind = str(row["kind"])
        plain = kind.split(":", 1)[0]
        key = (str(row["shown_at"])[:19], plain, str(row["service"]))
        note = notes.get(key)
        if note:
            row["ranker_note"] = note
            row["ranker_pages"] = False


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


def _print_report(report) -> None:
    d = report.to_dict()
    s = d["summary"]
    print(f"Data: {s['series']} series, {s['points']:,} points ({s['sampling']} sampling), "
          f"{s['events']:,} events, {s['incidents']} incidents "
          f"({s['train_incidents']} to tune on, {s['test_incidents']} to score over {s['test_days']} days).")
    print("Columns used: " + "; ".join(f"{k}: {v}" for k, v in d["mapping"].items() if v))
    for kind in ("jump", "delta", "static"):
        st = d["settings"][kind]
        flag = "  <- your queue" if d["settings"]["chosen"] == kind else ""
        print(f"  {kind:6s} {st['label']}{' (tuned)' if st.get('tuned') else ' (default)'}{flag}")
    rows = pd.DataFrame(d["table"])
    if not rows.empty and rows["detection_rate"].notna().any():
        rows["caught"] = rows["faults_caught"].astype(str) + "/" + rows["faults"].astype(str)
        cols = ["budget_k", "queue", "caught", "detection_rate", "items_per_day", "false_items_per_day", "oracle_ceiling"]
        print(rows[cols].to_string(index=False))
    if d["checks"]:
        print(f"Random detector (chance line): {d['checks']['chance_detection']:.0%}.  "
              f"Misses: {d['misses']}")
    for note in d["notes"]:
        print("Note: " + note)


def _eval_args(args):
    from src.generic import evaluate

    budgets = tuple(int(x) for x in str(args.budgets).split(","))
    return evaluate(args.metrics, args.incidents, args.events, budgets=budgets,
                    train_fraction=args.train_fraction, day_offset_hours=args.day_offset_hours or 0.0)


def evaluate_cmd(args: argparse.Namespace) -> int:
    import json

    report = _eval_args(args)
    _print_report(report)
    if args.out:
        Path(args.out).write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        print(f"Report written to {args.out}")
    return 0


def tune_cmd(args: argparse.Namespace) -> int:
    from src.generic import live_setting

    from .settings import save_saved, settings_path

    report = _eval_args(args)
    _print_report(report)
    saved = save_saved(live_setting(args.metrics, report))
    print(f"Saved live settings ({saved['label']}) to {settings_path()}. "
          "The web app and `cli score` now use them; delete the file to go back to the built-in rule.")
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
    for name, helptext in (("evaluate", "tune on the early part of YOUR data and report how every system does on the rest"),
                           ("tune", "evaluate, then save the best settings for the web app and `score`")):
        e = sub.add_parser(name, help=helptext)
        e.add_argument("--metrics", required=True, help="timestamp,value[,series] csv (any column names; long or wide)")
        e.add_argument("--incidents", help="past problems: start[,end][,series] csv")
        e.add_argument("--events", help="alerts / notifications / log lines csv")
        e.add_argument("--budgets", default="5,10,20", help="items a day to test, e.g. 5,10,20")
        e.add_argument("--train-fraction", type=float, default=0.3, help="share of each series used to tune")
        e.add_argument("--day-offset-hours", type=float)
        if name == "evaluate":
            e.add_argument("--out", help="write the full report as JSON")
    args = parser.parse_args(argv)
    if args.command == "evaluate":
        return evaluate_cmd(args)
    if args.command == "tune":
        return tune_cmd(args)
    return score(args)


if __name__ == "__main__":
    raise SystemExit(main())
