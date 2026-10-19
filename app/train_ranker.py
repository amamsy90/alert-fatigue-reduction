"""Train the optional learned ranker on the first week.

Labels are the existing catch rule, and only training-window items and faults
are used. The saved file is JSON (pointwise coefficients, numeric trees, or a
tiny MLP). It does not page.

CPU (no torch, no xgboost): ``--backend pointwise``.

RTX 5090, after the torch/xgboost install in docs/ML_MODELS.md:

    python train_ranker.py --data gaia --backend xgboost --device cuda --out results/ranker.json
    python train_ranker.py --data gaia --backend xgboost --device cuda \\
        --metric-model results/metric_model --out results/ranker_both.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.baseline import baseline_decisions
from src.config import Config
from src.experiment import load_gaia
from src.memory_spikes import detect_gaps, detect_jumps, detect_leaks
from src.metric_model import load_metric_model
from src.queue import alert_items, spike_items, tune_confirm_thresholds
from src.ranker import fit_ranker, save_ranker
from src.sample_data import generate_multivariate, load_multivariate
from src.tracking import record_run


def _load(which: str, days: int, seed: int):
    if which == "gaia":
        if not (Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()):
            raise SystemExit("GAIA folders are not in this run. Train on the laptop, or pass --data synthetic.")
        data = load_gaia()
        return data.metrics, data.alerts, data.faults, "gaia"
    folder = Path(tempfile.mkdtemp(prefix="ranker-"))
    generate_multivariate(folder, seed=seed, days=days)
    metrics, alerts, faults = load_multivariate(folder)
    return metrics, alerts, faults, "synthetic"


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the learned ranker on the first week.")
    parser.add_argument("--data", choices=("synthetic", "gaia"), required=True)
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--metric-model", default=None, help="directory of a trained metric model")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--out", required=True, help="path of ranker.json")
    args = parser.parse_args()
    metrics, alerts, faults, dataset = _load(args.data, args.days, args.seed)
    metrics = metrics.copy()
    metrics["timestamp"] = pd.to_datetime(metrics["timestamp"], utc=True)
    config = Config()
    origin = metrics["timestamp"].min()
    train_end = origin + pd.Timedelta(days=config.split_day)
    jumps = detect_jumps(metrics, config, detect_until=train_end)
    leaks = detect_leaks(metrics, config, detect_until=train_end)
    gaps = detect_gaps(metrics, config, detect_until=train_end)
    train_faults = [fault for fault in faults if fault.start < train_end]
    thr, _by_series = tune_confirm_thresholds(jumps, train_faults, config, train_until=train_end)
    tuned = replace(config, confirm_min_rise=thr)
    train_alerts = [alert for alert in alerts if alert.timestamp < train_end]
    items = (
        spike_items(jumps, tuned)
        + spike_items(leaks, tuned)
        + spike_items(gaps, tuned)
        + alert_items(train_alerts, baseline_decisions(train_alerts, tuned))
    )
    scores = None
    if args.metric_model:
        archive, status = load_metric_model(Path(args.metric_model))
        if archive is None:
            raise SystemExit(f"metric model did not load ({status})")
        scores = archive.score(metrics, end=train_end)
    ranker, status = fit_ranker(
        items,
        train_faults,
        tuned,
        scores,
        train_end=train_end,
        backend=args.backend,
        device=args.device,
        seed=args.seed,
    )
    if ranker is None:
        print(json.dumps({"status": status, "dataset": dataset}))
        sys.exit(1)
    digest = save_ranker(ranker, Path(args.out))
    summary = {
        "status": status,
        "dataset": dataset,
        "gaia": "pending: not scored here" if dataset != "gaia" else "weights only; run run_model_ablation.py to score",
        "backend": ranker.backend,
        "device": ranker.device,
        "fell_back": ranker.fell_back,
        "uses_metric_score": ranker.uses_metric_score,
        "threshold": ranker.threshold,
        "sha256": digest,
        "pages": False,
        "train_end": str(train_end),
        "versions": ranker.versions,
    }
    print(json.dumps(summary, indent=2))
    record_run(
        "train_ranker",
        config=config,
        metrics={"backend": ranker.backend, "device": ranker.device, "uses_metric_score": ranker.uses_metric_score},
        note=f"{dataset} {ranker.backend} {ranker.device}",
    )


if __name__ == "__main__":
    main()
