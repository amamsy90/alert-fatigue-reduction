"""Template-feature ablation with the contrastive encoder.

The candidate is the current queue plus template columns from the InfoNCE
encoder, trained on the first week only. Keep only when the 95% bootstrap
interval on the per-fault catch difference lies entirely above zero. The
default queue is unchanged. GAIA numbers are written only when GAIA is on disk.

About 82% of GAIA faults have no log line, so a template encoder can move at
most the other 18%. Hash-v1 and MiniLM are also projected to 4 numbers. This
encoder can use ``--dim``; that width is not applied to the other encoders.

    python run_log_encoder_ablation.py --data synthetic --dim 4 --epochs 15
    python run_log_encoder_ablation.py --data gaia --dim 16 --device cuda --epochs 8
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import pandas as pd

from run_classifier_ablation import evaluate_template_features
from src.config import Config
from src.experiment import load_gaia
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.log_encoder import ContrastiveEncoder, training_groups
from src.queue import n_days_between
from src.sample_data import generate
from src.tracking import record_run

LOG_COVERAGE = (
    "On GAIA, 82% of injected faults never write a log line, so a template encoder "
    "can affect at most the other 18%. This run did not remeasure that share."
)
PROJECTION = (
    "hash-v1 and the sentence encoder are projected to 4 numbers. "
    "LOG_ENCODER_DIM changes the width of the contrastive encoder only."
)


def _frames(which: str, seed: int):
    if which == "gaia":
        if not (Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()):
            raise SystemExit(
                "GAIA folders are not in this run. Nothing was scored. "
                "On the laptop: python run_log_encoder_ablation.py --data gaia --device cuda --dim 16"
            )
        data = load_gaia()
        return data.alerts, data.faults, "gaia"
    folder = Path(tempfile.mkdtemp(prefix="log-encoder-ablation-"))
    generate(folder, seed=seed, days=14)
    alerts, faults = align_alerts_to_faults(load_alerts(folder / "alerts.csv"), load_faults(folder / "faults.csv"))
    return alerts, faults, "synthetic"


def main() -> None:
    parser = argparse.ArgumentParser(description="Contrastive template-encoder ablation.")
    parser.add_argument("--data", choices=("synthetic", "gaia"), default="synthetic")
    parser.add_argument("--dim", type=int, default=4)
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--budget-k", type=int, default=20)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    alerts, faults, dataset = _frames(args.data, args.seed)
    config = Config(budget_k=args.budget_k)
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    _train_faults, test_faults = time_split(faults, config.split_day)
    origin = min(alert.timestamp for alert in alerts)
    boundary = origin + pd.Timedelta(days=config.split_day)
    end = max(alert.timestamp for alert in test_alerts) + pd.Timedelta(seconds=1)
    groups = training_groups(train_alerts, faults, boundary)
    try:
        from src.log_encoder_torch import resolve_torch_device, train_state
    except ImportError:
        report = {
            "dataset": dataset,
            "gaia": "pending: this run did not score GAIA" if dataset != "gaia" else "not scored: torch is not installed",
            "decision": "skipped",
            "reason": "torch-missing",
            "log_coverage": LOG_COVERAGE,
            "projection": PROJECTION,
            "dim": args.dim,
            "pages": False,
            "flags_changed": False,
        }
        _write(report, args.out, dataset)
        print(json.dumps({"decision": "skipped", "reason": "torch-missing"}))
        return
    device, fell_back = resolve_torch_device(args.device)
    try:
        state = train_state(groups, dim=args.dim, epochs=args.epochs, seed=args.seed, device=device)
    except ValueError as exc:
        report = {
            "dataset": dataset,
            "gaia": "pending: this run did not score GAIA" if dataset != "gaia" else "not scored",
            "decision": "skipped",
            "reason": str(exc),
            "log_coverage": LOG_COVERAGE,
            "projection": PROJECTION,
            "dim": args.dim,
            "pages": False,
            "flags_changed": False,
            "train_groups": len(groups),
        }
        _write(report, args.out, dataset)
        print(json.dumps({"decision": "skipped", "reason": str(exc), "train_groups": len(groups)}))
        return
    encoder = ContrastiveEncoder(state, dim=args.dim, name=f"contrastive-dim{args.dim}")
    report = evaluate_template_features(
        train_alerts,
        test_alerts,
        test_faults,
        config,
        n_days=n_days_between(boundary, end, config),
        dataset=dataset,
        encoder=encoder,
    )
    report["gaia"] = (
        "scored" if dataset == "gaia" else "pending: this run did not score GAIA"
    )
    report["log_coverage"] = LOG_COVERAGE
    report["projection"] = PROJECTION
    report["dim"] = args.dim
    report["device"] = device
    report["fell_back"] = fell_back
    report["train_groups"] = len(groups)
    report["pages"] = False
    report["flags_changed"] = False
    report["headline_k"] = args.budget_k
    out = _write(report, args.out, dataset)
    print(
        f"K={args.budget_k} dim={args.dim} decision={report['decision']} "
        f"caught={report['baseline_faults_caught']}->{report['candidate_faults_caught']} "
        f"delta={report.get('per_fault_catch_delta')}"
    )
    record_run(
        f"log_encoder_{dataset}",
        config=config,
        outputs=[out],
        metrics={"decision": report["decision"], "dim": args.dim},
        note=report["gaia"],
    )


def _write(report: dict, out: str | None, dataset: str) -> Path:
    path = Path(out) if out else Path("results") / f"log_encoder_{dataset}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    main()
