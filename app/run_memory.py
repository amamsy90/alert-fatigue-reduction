"""Score memory spikes against GAIA injected faults.

Usage:
    python run_memory.py --sample
    python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run

A metrics folder holds one csv per series, with columns timestamp and value.
The file name must contain the service (dbservice1, ...) and "memory".
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.config import Config
from src.gaia_loader import load_run_folder
from src.ingestion import time_split
from src.memory_spikes import detect_jumps, detect_spikes, load_metric_folder, score_spikes


def _sample_metrics(config: Config) -> tuple[pd.DataFrame, list]:
    """Flat week 1, one real jump in week 2, plus a single-point blip."""
    from src.entities import Fault

    origin = pd.Timestamp("2026-01-05", tz="UTC")
    rows = []
    for minute in range(0, 14 * 24 * 60, 5):
        rows.append((origin + pd.Timedelta(minutes=minute), 100.0))
    jump = origin + pd.Timedelta(days=config.split_day, hours=3)
    for offset in range(0, 11):
        rows.append((jump + pd.Timedelta(minutes=offset), 800.0))
    rows.append((jump + pd.Timedelta(hours=6), 800.0))
    frame = pd.DataFrame(rows, columns=["timestamp", "value"])
    frame["service"] = "dbservice1"
    frame["series_id"] = "dbservice1_memory"
    fault = Fault("f1", jump, jump + pd.Timedelta(minutes=10), "dbservice1", "memory")
    return frame, [fault]


def _sweep_jump_sizes(metrics_dir: str, gaia_run: str, config: Config) -> None:
    """Pick the jump size on week 1; week 2 is shown only to report it."""
    metrics = load_metric_folder(metrics_dir)
    faults = load_run_folder(gaia_run)[1]
    origin = min(fault.start for fault in faults)
    boundary = origin + pd.Timedelta(days=config.split_day)
    week1, week2 = time_split(faults, config.split_day)
    rows = []
    for gb in (0.3, 0.5, 0.8, 1.0, 1.5, 2.0):
        trial = replace(config, memory_jump_bytes=gb * 1e9)
        s1 = score_spikes(
            detect_jumps(metrics, trial, detect_from=origin, detect_until=boundary), week1
        )
        s2 = score_spikes(detect_jumps(metrics, trial, detect_from=boundary), week2)
        rows.append(
            {
                "jump_gb": gb,
                "week1_rate": s1["detection_rate"],
                "week1_false": s1["false_spikes"],
                "week2_rate": s2["detection_rate"],
                "week2_false": s2["false_spikes"],
            }
        )
        print(
            f"{gb:.1f} GB  week1 {s1['detection_rate']:.1%} false={int(s1['false_spikes'])}"
            f"   week2 {s2['detection_rate']:.1%} false={int(s2['false_spikes'])}"
        )
    out = Path("results")
    out.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "memory_jump_sweep.csv", index=False)
    print(f"Results written to {out / 'memory_jump_sweep.csv'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--metrics", default="data/gaia_metrics")
    parser.add_argument("--gaia-run", default=None)
    parser.add_argument(
        "--rule",
        choices=("std", "jump", "both"),
        default="std",
        help="std: week-1 mean + k*std; jump: rise over recent median; both: union",
    )
    parser.add_argument("--jump-gb", type=float, default=None)
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="score the jump rule at several sizes on week 1 and week 2 (GAIA only)",
    )
    args = parser.parse_args()
    config = Config()
    if args.jump_gb is not None:
        config.memory_jump_bytes = args.jump_gb * 1e9
    if args.sweep:
        _sweep_jump_sizes(args.metrics, args.gaia_run, config)
        return

    if args.sample:
        metrics, faults = _sample_metrics(config)
        tag = "sample"
    else:
        metrics = load_metric_folder(args.metrics)
        faults = load_run_folder(args.gaia_run)[1]
        origin = min(fault.start for fault in faults)
        boundary = origin + pd.Timedelta(days=config.split_day)
        _, faults = time_split(faults, config.split_day)
        tag = "gaia"

    if args.sample:
        boundary = metrics["timestamp"].min() + pd.Timedelta(days=config.split_day)
    spikes = []
    if args.rule in ("std", "both"):
        spikes += detect_spikes(metrics, config, fit_until=boundary, detect_from=boundary)
    if args.rule in ("jump", "both"):
        spikes += detect_jumps(metrics, config, detect_from=boundary)
    scores = score_spikes(spikes, faults)
    scores["rule"] = args.rule
    if args.rule != "std":
        tag = f"{tag}_{args.rule}"
    print(
        f"Spikes={int(scores['spikes'])}  faults={int(scores['faults'])}  "
        f"detected={int(scores['faults_detected'])} ({scores['detection_rate']:.1%})  "
        f"false spikes={int(scores['false_spikes'])}"
    )
    out = Path("results")
    out.mkdir(exist_ok=True)
    out_path = out / f"memory_{tag}.csv"
    pd.DataFrame([scores]).to_csv(out_path, index=False)
    print(f"Results written to {out_path}")


if __name__ == "__main__":
    main()