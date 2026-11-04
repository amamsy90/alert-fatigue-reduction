#!/usr/bin/env python3
"""Rebuild the homelab alerts table from Prometheus ALERTS history.

The live collector (before the episodes fix) wrote one alerts.csv row per
collector pass for every alert that was still firing. This script reads
``ALERTS{alertstate="firing"}`` history from Prometheus and writes one row
per firing episode, using ``homelab.collect`` (same alert id, columns,
company/source mapping, severity and 120-second gap rule), so ``evaluate.py``
loads the output unchanged.

It reads Prometheus and, for the default ``--start``, the live alerts.csv.
It writes ``--out`` only, via a sibling temp file and ``os.replace``. It does
not delete or rewrite the live collector files.

From ``homelab/``::

    ./scripts/run-py.sh -m homelab.rebuild_alerts --out /tmp/alerts_rebuilt.csv
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import collect
from .companies import load_companies

# Prometheus refuses query_range above 11,000 points per series. 5 days at 60 s is 7,200.
DEFAULT_CHUNK_HOURS = 120


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def data_dir(repo: Path) -> Path:
    return repo / "homelab" / "data"


def parse_time(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prom_get(url: str, path: str, params: dict, timeout: float = 120) -> dict:
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url.rstrip('/')}{path}?{query}", timeout=timeout) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        raise SystemExit(f"Prometheus error for {params}: {body}")
    return body


def merge_chunks(bodies: list[dict]) -> dict:
    """Merge query_range bodies into one matrix, one sample per timestamp."""
    samples: dict[tuple, dict[float, str]] = defaultdict(dict)
    metrics: dict[tuple, dict] = {}
    for body in bodies:
        for series in (body.get("data") or {}).get("result") or []:
            metric = series.get("metric") or {}
            key = tuple(sorted(metric.items()))
            metrics[key] = metric
            for stamp, value in series.get("values") or []:
                samples[key][float(stamp)] = value
    result = [
        {"metric": metrics[key], "values": [[stamp, samples[key][stamp]] for stamp in sorted(samples[key])]}
        for key in metrics
    ]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def episodes_from_matrix(payload: dict, companies: list) -> pd.DataFrame:
    """One row per firing episode over a whole history. No open-episode state."""
    rows = collect.alerts_from_prometheus(payload, companies)
    frame = pd.DataFrame(rows, columns=list(collect.ALERT_COLUMNS))
    if frame.empty:
        return frame
    frame = frame.drop_duplicates(["alert_id"], keep="last")
    return frame.sort_values(["timestamp", "source", "alert_type"]).reset_index(drop=True)


def fetch_history(prom_url: str, query: str, start: float, end: float, step: int, chunk_hours: float) -> dict:
    """query_range in chunks; merge samples per full label set. Returns a Prometheus-shaped payload."""
    chunk = int(chunk_hours * 3600)
    if chunk / step > 11000:
        raise SystemExit(f"chunk of {chunk_hours} h at step {step}s exceeds Prometheus' 11,000-point limit")
    bodies = []
    t = start
    while t <= end:
        stop = min(t + chunk - step, end)
        bodies.append(prom_get(prom_url, "/api/v1/query_range", {"query": query, "start": t, "end": stop, "step": f"{step}s"}))
        t = stop + step
    return merge_chunks(bodies)


def earliest_sample(prom_url: str) -> float | None:
    body = prom_get(prom_url, "/api/v1/query", {"query": "prometheus_tsdb_lowest_timestamp_seconds"})
    values = [float(item["value"][1]) for item in body["data"]["result"]]
    return min(values) if values else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=None, help="repository root; default is this checkout")
    parser.add_argument("--prom-url", default=None, help="default: PROM_URL from homelab/data/endpoints.env")
    parser.add_argument("--alerts", type=Path, default=None, help="live alerts.csv, used for the default --start")
    parser.add_argument("--companies-file", type=Path, default=None)
    parser.add_argument("--start", default=None, help="ISO time or epoch; default: earliest timestamp in --alerts")
    parser.add_argument("--end", default=None, help="ISO time or epoch; default: now")
    parser.add_argument("--step", type=int, default=60, help="seconds; the collector uses 60")
    parser.add_argument("--chunk-hours", type=float, default=DEFAULT_CHUNK_HOURS)
    parser.add_argument(
        "--lead-minutes",
        type=float,
        default=60,
        help="also read this long before --start so an episode already firing at --start keeps its real start",
    )
    args = parser.parse_args(argv)

    repo = args.repo or repo_root()
    data = data_dir(repo)
    prom_url = args.prom_url
    if not prom_url:
        endpoints = data / "endpoints.env"
        if endpoints.exists():
            for line in endpoints.read_text(encoding="utf-8").splitlines():
                if line.startswith("PROM_URL="):
                    prom_url = line.split("=", 1)[1].strip().strip('"')
    prom_url = prom_url or "http://127.0.0.1:9090"
    alerts_path = args.alerts or data / "collected" / "alerts.csv"
    companies = load_companies(args.companies_file or repo / "homelab" / "companies.yaml")

    if args.start:
        start = parse_time(args.start)
    else:
        live = pd.read_csv(alerts_path, usecols=["timestamp"])
        start = pd.to_datetime(live["timestamp"], utc=True, format="mixed").min().timestamp()
    end = parse_time(args.end) if args.end else time.time()
    step = args.step
    read_from = (int(start - args.lead_minutes * 60) // step) * step
    end = (int(end) // step) * step

    payload = fetch_history(prom_url, collect.alerts_query(), read_from, end, step, args.chunk_hours)
    frame = episodes_from_matrix(payload, companies)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_name(f".{args.out.name}.tmp")
    frame.to_csv(tmp, index=False)
    os.replace(tmp, args.out)

    lowest = earliest_sample(prom_url)
    first_alert = min((float(v[0]) for s in payload["data"]["result"] for v in s["values"][:1]), default=None)
    summary = {
        "out": str(args.out),
        "prom_url": prom_url,
        "requested_start": iso(start),
        "read_from": iso(read_from),
        "end": iso(end),
        "step_seconds": step,
        "chunk_hours": args.chunk_hours,
        "gap_rule_seconds": collect.EPISODE_GAP_SECONDS,
        "prometheus_lowest_sample": iso(lowest) if lowest else None,
        "first_alerts_sample": iso(first_alert) if first_alert else None,
        "series": len(payload["data"]["result"]),
        "episodes": len(frame),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
