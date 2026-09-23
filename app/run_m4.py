"""M4 runner: budget-aware policy vs the threshold baseline (deliverable D4).

The escalate threshold is chosen on week 1. Week 2 is scored at each daily
budget. The gate is: higher fault detection, MTTA at least 50% lower, and
at least 80% fewer false alerts, at every K.

Usage:
    python run_m4.py --sample
    python run_m4.py --gaia-run data/gaia_raw/run/run
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config
from src.gaia_loader import load_run_folder
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.metrics import evaluate_all
from src.policy import bayes_threshold, meets_gate, policy_decisions, tune_threshold
from src.sample_data import generate


def load(args, config: Config):
    if args.gaia_run:
        return (*load_run_folder(args.gaia_run), "gaia")
    data_dir = Path("data/sample") if args.sample else Path(args.data)
    if args.sample:
        generate(data_dir, seed=config.random_seed)
    alerts = load_alerts(data_dir / "alerts.csv")
    faults = load_faults(data_dir / "faults.csv")
    return (*align_alerts_to_faults(alerts, faults), "sample" if args.sample else "csv")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/gaia")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--gaia-run", default=None)
    args = parser.parse_args()

    config = Config()
    alerts, faults, tag = load(args, config)
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    _, test_faults = time_split(faults, config.split_day)
    model = AlertClassifier(config).fit(train_alerts)
    train_proba = model.predict_proba(train_alerts)
    threshold = tune_threshold(train_alerts, train_proba, config)
    print(
        f"Bayes threshold {bayes_threshold(config):.3f}. "
        f"Week-1 cost threshold {threshold:.3f}."
    )
    test_proba = model.predict_proba(test_alerts)

    rows = []
    gates = []
    for k in config.budget_sweep:
        cfg = Config(budget_k=k)
        base = evaluate_all(
            "baseline", baseline_decisions(test_alerts, cfg), test_alerts, test_faults,
            miss_cost=cfg.miss_cost,
        )
        learned = evaluate_all(
            "policy", policy_decisions(test_alerts, test_proba, cfg, threshold),
            test_alerts, test_faults, miss_cost=cfg.miss_cost,
        )
        gate = meets_gate(learned, base)
        gates.append(gate)
        for row in (base, learned):
            row["budget_k"] = k
            row["gate"] = gate
            rows.append(row)
        print(
            f"K={k:2d}  baseline det/FA/day/MTTA "
            f"{base['detection_rate']:.3f}/{base['false_alerts_per_day']}/{base['mtta_minutes']}  "
            f"policy {learned['detection_rate']:.3f}/{learned['false_alerts_per_day']}/"
            f"{learned['mtta_minutes']}  gate={gate}"
        )

    print(f"M4 gate at every budget: {all(gates)}")
    out = Path("results")
    out.mkdir(exist_ok=True)
    out_path = out / f"m4_{tag}.csv"
    pd.DataFrame(rows)[
        ["system", "budget_k", "surfaced", "precision", "recall", "detection_rate",
         "mtta_minutes", "false_alerts_per_day", "cost_weighted_error", "reduction_rate", "gate"]
    ].to_csv(out_path, index=False)
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
