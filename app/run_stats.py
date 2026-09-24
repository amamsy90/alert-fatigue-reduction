"""Defend the headline numbers: interval, test vs baseline, why faults are missed.

Reads the saved walk-forward table. Reloads GAIA only for the per-service
and silent-fault counts (no retraining).

Usage:
    python run_stats.py
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd

from src.config import Config
from src.eval_stats import (
    bootstrap_mean_ci,
    by_service_rows,
    sign_test_greater,
    signal_counts,
    week_pressure,
)
from src.gaia_loader import load_run_folder
from src.ingestion import time_split
from src.memory_spikes import detect_jumps, load_metric_folder


def _walk_forward_stats(path: Path) -> dict:
    walk = pd.read_csv(path)
    queue = walk.loc[walk["queue"] == "shared_jumps_1st"].sort_values("fold")
    base = walk.loc[walk["queue"] == "baseline"].sort_values("fold")
    rates = [float(x) for x in queue["detection_rate"]]
    base_rates = [float(x) for x in base["detection_rate"]]
    interval = bootstrap_mean_ci(rates)
    test = sign_test_greater(rates, base_rates)
    return {
        "folds": interval.n,
        "mean": round(interval.mean, 4),
        "ci_low": round(interval.low, 4),
        "ci_high": round(interval.high, 4),
        "min": round(min(rates), 4),
        "max": round(max(rates), 4),
        "wins_vs_baseline": test.wins,
        "sign_n": test.n,
        "sign_p": test.p_value,
        "weakest_fold": int(queue.loc[queue["detection_rate"].idxmin(), "fold"]),
        "weakest_faults": int(queue.loc[queue["detection_rate"].idxmin(), "faults"]),
    }


def main() -> None:
    config = Config()
    walk_path = Path("results") / "walkforward_gaia.csv"
    wf = _walk_forward_stats(walk_path)
    print(
        f"Walk-forward mean {wf['mean']:.1%} "
        f"(95% bootstrap CI {wf['ci_low']:.1%}-{wf['ci_high']:.1%}) "
        f"over {wf['folds']} folds."
    )
    print(
        f"Queue > baseline in {wf['wins_vs_baseline']}/{wf['sign_n']} folds "
        f"(p = {wf['sign_p']:.4f} if each fold is a coin flip)."
    )
    print(
        f"Weakest week is fold {wf['weakest_fold']} "
        f"({wf['min']:.1%}, {wf['weakest_faults']} faults)."
    )

    alerts, faults = load_run_folder("data/gaia_raw/run/run")
    metrics = load_metric_folder("data/gaia_metrics")
    tags = Counter(f.description for f in faults)
    print(f"Injection tags: {dict(tags)}")

    _, test_alerts = time_split(alerts, config.split_day)
    _, test_faults = time_split(faults, config.split_day)
    origin = min(a.timestamp for a in alerts)
    split_at = origin + pd.Timedelta(days=config.split_day)
    test_jumps = detect_jumps(metrics, config, detect_from=split_at)

    signals = signal_counts(test_faults, test_alerts, test_jumps)
    n = len(test_faults)
    print("Test faults by signal (why a log-only system cannot hit 95%):")
    for key in ("log_and_jump", "jump_only", "log_only", "silent"):
        print(f"  {key:13s} {signals[key]:4d}  ({signals[key] / n:.1%})")

    service_rows = by_service_rows(test_faults, test_alerts, test_jumps)
    print("Test faults by service:")
    for row in service_rows:
        print(
            f"  {row['service']:14s} faults {row['faults']:4d}  "
            f"log {row['log_rate']:.1%}  jump {row['jump_rate']:.1%}"
        )

    # Fold 7 is 19–26 Aug in the saved table. Explain the pressure, not a new score.
    weak_start = pd.Timestamp("2021-08-19", tz="UTC")
    weak_end = pd.Timestamp("2021-08-26", tz="UTC")
    pressure = week_pressure(test_faults, weak_start, weak_end)
    other = [f for f in test_faults if not (weak_start <= f.start < weak_end)]
    other_days = max(1, (max(f.start for f in other).normalize() - min(f.start for f in other).normalize()).days)
    print(
        f"Fold 7 pressure: {pressure['faults']} faults in {pressure['days']} days "
        f"({pressure['faults_per_day']}/day vs budget K=20). "
        f"Other test days: {len(other) / other_days:.1f} faults/day."
    )

    out = Path("results")
    out.mkdir(exist_ok=True)
    headline = {
        **wf,
        "test_faults": n,
        **{f"signal_{k}": v for k, v in signals.items()},
        "injection_tags": ";".join(f"{k}:{v}" for k, v in tags.items()),
        "fold7_faults_per_day": pressure["faults_per_day"],
    }
    pd.DataFrame([headline]).to_csv(out / "stats_gaia.csv", index=False)
    pd.DataFrame(service_rows).to_csv(out / "stats_by_service.csv", index=False)
    print(f"Wrote {out / 'stats_gaia.csv'} and {out / 'stats_by_service.csv'}")


if __name__ == "__main__":
    main()
