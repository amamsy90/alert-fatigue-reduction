"""Train the optional trace model. Does not page and does not set TRACE_MODEL.

    python train_trace_model.py --data synthetic --device cpu --backend plain --epochs 20 --out results/trace_model
    python train_trace_model.py --data rcaeval --device cuda --backend auto --epochs 8 --out results/trace_model
"""
from __future__ import annotations

import argparse
from pathlib import Path

from src.config import Config
from src.trace_eval import synthetic_traces
from src.trace_model import fit_trace_model, save_trace_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the optional trace root-cause model.")
    parser.add_argument("--data", choices=("synthetic", "rcaeval"), default="synthetic")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--backend", default="plain", choices=("auto", "plain", "pyg"))
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="results/trace_model")
    args = parser.parse_args()
    if args.data == "rcaeval":
        raise SystemExit(
            "Train on the laptop after traces are downloaded: "
            "python run_trace_eval.py --data rcaeval --device cuda --backend auto --epochs 8"
        )
    spans, _metrics, faults = synthetic_traces(seed=args.seed)
    origin = spans["time"].min()
    boundary = origin + pd_timedelta(Config().split_day)
    archive, status = fit_trace_model(
        spans,
        faults,
        boundary,
        epochs=args.epochs,
        seed=args.seed,
        device=args.device,
        backend=args.backend,
    )
    if archive is None:
        raise SystemExit(f"trace model did not train ({status})")
    digest = save_trace_model(archive, Path(args.out))
    print(f"status={status} backend={archive.backend} device={archive.device} sha256={digest} pages=false")


def pd_timedelta(days: int):
    import pandas as pd

    return pd.Timedelta(days=days)


if __name__ == "__main__":
    main()
