"""M1 runner: ingestion + alignment + baseline + metrics (deliverable D1).

Usage:
    python -m experiments.legacy_alerts_only.run_m1 --sample                 # smoke-test on synthetic data
    python -m experiments.legacy_alerts_only.run_m1 --data path/to/gaia      # real GAIA files

Writes results to app/results/m1_<tag>.csv and prints a summary table.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.baseline import baseline_decisions
from src.config import Config
from src.gaia_loader import load_run_folder
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.metrics import evaluate_all
from src.sample_data import generate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/gaia", help="folder with alerts.csv and faults.csv")
    parser.add_argument("--sample", action="store_true", help="generate and use synthetic sample data")
    parser.add_argument("--gaia-run", default=None,
                        help="folder of GAIA MicroSS run_table_*.csv files")
    args = parser.parse_args()

    config = Config()
    if args.gaia_run:
        alerts, faults = load_run_folder(args.gaia_run)
        tag = "gaia"
    else:
        data_dir = Path("data/sample") if args.sample else Path(args.data)
        if args.sample:
            generate(data_dir, seed=config.random_seed)
        alerts = load_alerts(data_dir / "alerts.csv")
        faults = load_faults(data_dir / "faults.csv")
        alerts, faults = align_alerts_to_faults(alerts, faults)
        tag = "sample" if args.sample else "gaia"

    train_alerts, test_alerts = time_split(alerts, config.split_day)
    train_faults, test_faults = time_split(faults, config.split_day)
    print(f"Loaded {len(alerts)} alerts, {len(faults)} faults "
          f"(train: {len(train_alerts)}/{len(train_faults)}, "
          f"test: {len(test_alerts)}/{len(test_faults)})")

    rows = []
    for k in config.budget_sweep:
        cfg = Config(budget_k=k)
        decisions = baseline_decisions(test_alerts, cfg)
        row = evaluate_all(f"baseline_k{k}", decisions, test_alerts, test_faults,
                           miss_cost=cfg.miss_cost)
        row["budget_k"] = k
        rows.append(row)

    results = pd.DataFrame(rows)[
        ["system", "budget_k", "alerts", "surfaced", "precision", "recall",
         "reduction_rate", "detection_rate", "mtta_minutes",
         "false_alerts_per_day", "cost_weighted_error"]
    ]
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"m1_{tag}.csv"
    results.to_csv(out_path, index=False)
    print(results.to_string(index=False))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
