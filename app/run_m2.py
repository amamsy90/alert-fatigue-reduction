"""M2 runner: cost-sensitive classifier vs the baseline (deliverable D2).

Trains on week 1 and scores week 2. At each daily budget K the classifier
must beat the baseline's precision and recall.

Usage:
    python run_m2.py --sample
    python run_m2.py --gaia-run data/gaia_raw/run/run
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier, classifier_decisions
from src.config import Config
from src.features import build_feature_frame
from src.gaia_loader import load_run_folder
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.metrics import evaluate_all
from src.sample_data import generate


def load(args, config: Config):
    if args.gaia_run:
        alerts, faults = load_run_folder(args.gaia_run)
        return alerts, faults, "gaia"
    data_dir = Path("data/sample") if args.sample else Path(args.data)
    if args.sample:
        generate(data_dir, seed=config.random_seed)
    alerts = load_alerts(data_dir / "alerts.csv")
    faults = load_faults(data_dir / "faults.csv")
    alerts, faults = align_alerts_to_faults(alerts, faults)
    return alerts, faults, "sample" if args.sample else "csv"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/gaia")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--gaia-run", default=None)
    args = parser.parse_args()

    config = Config()
    alerts, faults, tag = load(args, config)
    featured = build_feature_frame(alerts, config)
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    _, test_faults = time_split(faults, config.split_day)
    train_ids = {a.alert_id for a in train_alerts}
    train_positives = int(featured.loc[featured["alert_id"].isin(train_ids), "label"].sum())
    print(
        f"Train alerts: {len(train_alerts)} ({train_positives} inside a fault window). "
        f"Test alerts: {len(test_alerts)}, test faults: {len(test_faults)}"
    )

    model = AlertClassifier(config).fit(train_alerts)
    # Re-score the test alerts on their own history so the model is applied
    # the way it will run in production: no future rows, train-fitted weights.
    proba = model.predict_proba(test_alerts)

    rows = []
    for k in config.budget_sweep:
        cfg = Config(budget_k=k)
        base = evaluate_all(
            "baseline", baseline_decisions(test_alerts, cfg), test_alerts, test_faults,
            miss_cost=cfg.miss_cost,
        )
        learned = evaluate_all(
            "classifier", classifier_decisions(test_alerts, proba, cfg),
            test_alerts, test_faults, miss_cost=cfg.miss_cost,
        )
        for row in (base, learned):
            row["budget_k"] = k
            rows.append(row)
        beats = (
            learned["recall"] > base["recall"] and learned["precision"] >= base["precision"]
        )
        print(
            f"K={k:2d}  baseline P/R {base['precision']:.3f}/{base['recall']:.3f}  "
            f"classifier P/R {learned['precision']:.3f}/{learned['recall']:.3f}  "
            f"detection {learned['detection_rate']:.3f}  beats={beats}"
        )

    results = pd.DataFrame(rows)[
        ["system", "budget_k", "surfaced", "precision", "recall", "reduction_rate",
         "detection_rate", "mtta_minutes", "false_alerts_per_day", "cost_weighted_error"]
    ]
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"m2_{tag}.csv"
    results.to_csv(out_path, index=False)
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
