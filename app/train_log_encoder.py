"""Train the contrastive template encoder on the training window only.

Does not set LOG_ENCODER. The width defaults to 4, the same cap as hash-v1.
``--dim`` is for this encoder only.

    python train_log_encoder.py --data synthetic --dim 4 --device cpu --out results/log_encoder
    python train_log_encoder.py --data gaia --dim 16 --device cuda --epochs 8 --out results/log_encoder
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pandas as pd

from src.config import Config
from src.entities import Fault
from src.experiment import load_gaia
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults
from src.log_encoder import ContrastiveEncoder, save_encoder, training_groups
from src.sample_data import generate


def _synthetic(seed: int):
    folder = Path(tempfile.mkdtemp(prefix="log-encoder-"))
    generate(folder, seed=seed, days=14)
    alerts = load_alerts(folder / "alerts.csv")
    faults = load_faults(folder / "faults.csv")
    return align_alerts_to_faults(alerts, faults)


def _gaia():
    if not (Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()):
        raise SystemExit(
            "GAIA folders are not in this run. Nothing was trained. "
            "On the laptop: python train_log_encoder.py --data gaia --device cuda --dim 16"
        )
    data = load_gaia()
    return data.alerts, data.faults


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the contrastive log-template encoder.")
    parser.add_argument("--data", choices=("synthetic", "gaia"), default="synthetic")
    parser.add_argument("--dim", type=int, default=4)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="results/log_encoder")
    args = parser.parse_args()
    if args.dim < 1:
        raise SystemExit("--dim must be at least 1")
    alerts, faults = _synthetic(args.seed) if args.data == "synthetic" else _gaia()
    origin = min(alert.timestamp for alert in alerts)
    boundary = origin + pd.Timedelta(days=Config().split_day)
    groups = training_groups(alerts, [fault for fault in faults if isinstance(fault, Fault)], boundary)
    try:
        from src.log_encoder_torch import resolve_torch_device, train_state
    except ImportError as exc:
        raise SystemExit("torch is not installed. CPU CI does not train this encoder.") from exc
    device, fell_back = resolve_torch_device(args.device)
    state = train_state(groups, dim=args.dim, epochs=args.epochs, seed=args.seed, device=device)
    encoder = ContrastiveEncoder(state, dim=args.dim, name="contrastive")
    digest = save_encoder(
        encoder,
        Path(args.out),
        extra={"dataset": args.data, "device": device, "fell_back": fell_back, "train_groups": len(groups)},
    )
    print(f"groups={len(groups)} dim={args.dim} device={device} fell_back={fell_back} sha256={digest} pages=false")


if __name__ == "__main__":
    main()
