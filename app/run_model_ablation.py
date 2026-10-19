"""Compare the current queue with the optional metric model and learned ranker.

Variants: baseline, +metric model, +ranker, +both. Keep a variant only when the
95% bootstrap interval on the per-fault catch difference is entirely above
zero. Also prints false items a day and catch at K = 5, 10, 20 and 50.

``--walk-forward`` refits on the past only, one week at a time, at K = 20.

GAIA is not on the cloud VM. ``--data auto`` uses GAIA when both folders exist
and the synthetic sample otherwise. A synthetic run does not fill in GAIA
numbers, and it does not turn the flags on.

    python run_model_ablation.py --data synthetic --backend pointwise --epochs 2
    python run_model_ablation.py --data gaia --device cuda --backend xgboost --epochs 8 --walk-forward
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from src.config import Config
from src.experiment import load_gaia
from src.model_eval import EvalSpec, evaluate_models, walk_forward_models
from src.sample_data import generate_multivariate, load_multivariate
from src.tracking import record_run


def _gaia_ready() -> bool:
    return Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()


def main() -> None:
    parser = argparse.ArgumentParser(description="Time-split ablation of the optional models.")
    parser.add_argument("--data", choices=("auto", "synthetic", "gaia"), default="auto")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--freq", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    which = args.data
    if which == "auto":
        which = "gaia" if _gaia_ready() else "synthetic"
    if which == "gaia" and not _gaia_ready():
        raise SystemExit(
            "GAIA folders are not in this run (data/gaia_raw and data/gaia_metrics). "
            "Nothing was scored. On the laptop: python run_model_ablation.py --data gaia --device cuda --backend xgboost"
        )
    if which == "gaia":
        data = load_gaia()
        metrics, alerts, faults = data.metrics, data.alerts, data.faults
        freq = args.freq or "30s"
    else:
        folder = Path(tempfile.mkdtemp(prefix="model-ablation-"))
        generate_multivariate(folder, seed=args.seed, days=21 if args.walk_forward else 14, freq="1min")
        metrics, alerts, faults = load_multivariate(folder)
        freq = args.freq or "1min"
    spec = EvalSpec(
        freq=freq,
        epochs=args.epochs,
        device=args.device,
        backend=args.backend,
        seed=args.seed,
        window=32 if which == "gaia" else 16,
        stride=10 if which == "gaia" else 8,
        hidden=16 if which == "gaia" else 8,
    )
    report = evaluate_models(metrics, alerts, faults, config=Config(), spec=spec, dataset=which)
    if args.walk_forward:
        report["walk_forward"] = walk_forward_models(
            metrics, alerts, faults, config=Config(budget_k=20), spec=spec, dataset=which
        )
    else:
        report["walk_forward"] = None
    out = Path(args.out) if args.out else Path("results") / f"model_ablation_{which}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dataset": report["dataset"], "gaia": report["gaia"], "out": str(out)}, indent=2))
    headline = report["headline"]
    for name in ("baseline", "metric_model", "ranker", "both"):
        row = headline[name]
        caught = row.get("faults_caught")
        false_items = row.get("false_items_per_day")
        print(
            f"K={report['headline_k']} {name:14s} decision={row.get('decision')} "
            f"caught={caught} false/day={false_items} delta={row.get('per_fault_catch_delta')}"
        )
    record_run(
        f"model_ablation_{which}",
        config=Config(),
        outputs=[out],
        metrics={
            "dataset": which,
            "headline_k": report["headline_k"],
            "metric_model": report["metric_model"]["status"],
        },
        note=report["gaia"],
    )


if __name__ == "__main__":
    main()
