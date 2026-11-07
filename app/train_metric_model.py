"""Train the optional metric model on the first week and save a safetensors archive.

The queue is unchanged: this only writes weights. GAIA is used when
``--data gaia`` and the folders are present. Otherwise pass ``--data synthetic``.

CPU:

    uv sync --group dev --group ml
    uv pip install torch
    python train_metric_model.py --data synthetic --device cpu --out results/metric_model

RTX 5090 (torch cu130, sm_120), after ``uv sync --group dev --group gpu --group ml``:

    uv pip install --index-url https://download.pytorch.org/whl/cu130 \\
        --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match \\
        torch safetensors
    python train_metric_model.py --data gaia --device cuda --epochs 8 --out results/metric_model
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from src.config import Config
from src.experiment import load_gaia
from src.metric_model import fit_metric_model, save_metric_model
from src.sample_data import generate_multivariate, load_multivariate
from src.tracking import record_run


def _load(which: str, days: int, seed: int):
    if which == "gaia":
        if not (Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()):
            raise SystemExit("GAIA folders are not in this run. Train on the laptop, or pass --data synthetic.")
        data = load_gaia()
        return data.metrics, data.alerts, data.faults, "30s", "gaia"
    folder = Path(tempfile.mkdtemp(prefix="metric-model-"))
    generate_multivariate(folder, seed=seed, days=days, freq="1min")
    metrics, alerts, faults = load_multivariate(folder)
    return metrics, alerts, faults, "1min", "synthetic"


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the causal metric TCN on the first week.")
    parser.add_argument("--data", choices=("synthetic", "gaia"), required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--window", type=int, default=32)
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--hidden", type=int, default=16)
    parser.add_argument("--freq", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--out", required=True, help="directory for model.safetensors and model.json")
    args = parser.parse_args()
    metrics, _alerts, faults, default_freq, dataset = _load(args.data, args.days, args.seed)
    freq = args.freq or default_freq
    origin = pd.to_datetime(metrics["timestamp"], utc=True).min()
    config = Config()
    train_end = origin + pd.Timedelta(days=config.split_day)
    archive, status = fit_metric_model(
        metrics,
        faults,
        train_end,
        freq=freq,
        window=args.window,
        stride=args.stride,
        hidden=args.hidden,
        epochs=args.epochs,
        seed=args.seed,
        device=args.device,
    )
    if archive is None:
        print(json.dumps({"status": status, "dataset": dataset, "gaia": "pending" if dataset != "gaia" else "scored"}))
        sys.exit(1)
    digest = save_metric_model(archive, Path(args.out))
    summary = {
        "status": status,
        "dataset": dataset,
        "gaia": "pending: not scored here" if dataset != "gaia" else "weights only; run python -m experiments.model_ablation.run_model_ablation to score",
        "device": archive.device,
        "device_requested": archive.device_requested,
        "fell_back": archive.fell_back,
        "channels": archive.channels,
        "threshold": None if archive.threshold == float("inf") else archive.threshold,
        "weights_sha256": digest,
        "pages": False,
        "train_end": str(train_end),
        "versions": archive.versions,
    }
    print(json.dumps(summary, indent=2))
    record_run(
        "train_metric_model",
        config=config,
        metrics={"device": archive.device, "channels": len(archive.channels), "fell_back": archive.fell_back},
        note=f"{dataset} {archive.device} sha256={digest[:16]}",
    )


if __name__ == "__main__":
    main()
