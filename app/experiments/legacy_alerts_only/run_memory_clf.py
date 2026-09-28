"""Feed memory-spike episodes into the classifier and rescore week 2.

Week-1 spikes are found from days 4-7 only, using a baseline fit on days 0-4,
so the test week never enters training. Week-2 spikes use the full week-1
baseline. The cost threshold is still chosen on week 1. Week 2 is scored
against the severity-threshold baseline at each daily budget.

Usage:
    python -m experiments.legacy_alerts_only.run_memory_clf
    python -m experiments.legacy_alerts_only.run_memory_clf --rule jump   # rise over recent median, all of week 1
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config
from src.features import build_feature_frame
from src.gaia_loader import load_run_folder
from src.ingestion import time_split
from src.memory_spikes import detect_jumps, detect_spikes, load_metric_folder
from src.metrics import evaluate_all
from src.policy import policy_decisions, tune_threshold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rule",
        choices=("std", "jump"),
        default="std",
        help="std: mean + k*std against a fitted window; jump: rise over recent median",
    )
    args = parser.parse_args()
    config = Config()
    alerts, faults = load_run_folder("data/gaia_raw/run/run")
    metrics = load_metric_folder("data/gaia_metrics")
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    _, test_faults = time_split(faults, config.split_day)

    origin = min(alert.timestamp for alert in alerts)
    split_at = origin + pd.Timedelta(days=config.split_day)
    mid = origin + pd.Timedelta(days=4)
    if args.rule == "jump":
        train_spikes = detect_jumps(metrics, config, detect_from=origin, detect_until=split_at)
        test_spikes = detect_jumps(metrics, config, detect_from=split_at)
    else:
        train_spikes = detect_spikes(
            metrics, config, fit_until=mid, detect_from=mid, detect_until=split_at
        )
        test_spikes = detect_spikes(
            metrics, config, fit_until=split_at, detect_from=split_at
        )
    train_marked = int(build_feature_frame(train_alerts, config, train_spikes)["during_memory_spike"].sum())
    test_marked = int(build_feature_frame(test_alerts, config, test_spikes)["during_memory_spike"].sum())
    print(
        f"Train spikes={len(train_spikes)} marked alerts={train_marked}. "
        f"Test spikes={len(test_spikes)} marked alerts={test_marked}."
    )

    model = AlertClassifier(config).fit(train_alerts, train_spikes)
    threshold = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_spikes), config)
    test_proba = model.predict_proba(test_alerts, test_spikes)
    print(f"Week-1 cost threshold {threshold:.3f}.")

    rows = []
    for k in config.budget_sweep:
        cfg = Config(budget_k=k)
        base = evaluate_all(
            "baseline",
            baseline_decisions(test_alerts, cfg),
            test_alerts,
            test_faults,
            miss_cost=cfg.miss_cost,
        )
        learned = evaluate_all(
            "memory_policy",
            policy_decisions(test_alerts, test_proba, cfg, threshold),
            test_alerts,
            test_faults,
            miss_cost=cfg.miss_cost,
        )
        for row in (base, learned):
            row["budget_k"] = k
            rows.append(row)
        print(
            f"K={k:2d}  baseline det/recall/FA/day "
            f"{base['detection_rate']:.3f}/{base['recall']:.3f}/{base['false_alerts_per_day']}  "
            f"memory {learned['detection_rate']:.3f}/{learned['recall']:.3f}/"
            f"{learned['false_alerts_per_day']}  "
            f"cost {base['cost_weighted_error']:.0f} -> {learned['cost_weighted_error']:.0f}"
        )

    suffix = "" if args.rule == "std" else f"_{args.rule}"
    out = Path("results") / f"memory_clf_gaia{suffix}.csv"
    pd.DataFrame(rows)[
        ["system", "budget_k", "surfaced", "precision", "recall", "detection_rate",
         "mtta_minutes", "false_alerts_per_day", "cost_weighted_error", "reduction_rate"]
    ].to_csv(out, index=False)
    print(f"Results written to {out}")


if __name__ == "__main__":
    main()
