"""Full-queue template ablation with the contrastive encoder.

The candidate is the published queue (metric items plus log alerts) with
template columns from the InfoNCE encoder. The baseline is that same queue
without template columns. Keep only when the 95% bootstrap interval on the
per-fault catch difference lies entirely above zero.

On GAIA the window is the published one: the first alert through one second
after the last alert (1223 held-out faults on the extract used by the 2b
ablation). Faults that start after the last alert are not scored. ``--seeds``
refits the encoder and the classifier for every seed and reports each one.
The default queue is unchanged. GAIA numbers are written only when GAIA is
on disk.

About 82% of GAIA faults have no log line, so a template encoder can move at
most the other 18%. Hash-v1 and MiniLM are also projected to 4 numbers. This
encoder can use ``--dim``; that width is not applied to the other encoders.

    python run_log_encoder_ablation.py --data synthetic --dim 4 --epochs 15
    python run_log_encoder_ablation.py --data gaia --dim 16 --device cuda --epochs 8 --seeds 42,0,1,2,3,7
"""
from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pandas as pd

from run_classifier_ablation import evaluate_template_features
from src.config import Config
from src.entities import Alert, Fault
from src.experiment import load_gaia
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults
from src.log_encoder import ContrastiveEncoder, training_groups
from src.memory_spikes import detect_gaps, detect_jumps, detect_leaks
from src.queue import n_days_between, spike_items, tune_confirm_thresholds
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


def queue_split(
    alerts: list[Alert],
    faults: list[Fault],
    origin: pd.Timestamp,
    last: pd.Timestamp,
    split_day: int,
) -> tuple[list[Alert], list[Alert], list[Fault], list[Fault], pd.Timestamp, pd.Timestamp]:
    """First week / later days, stopping one second after ``last``.

    ``last`` is the last alert. A fault that starts after that is not a test
    fault. Train and test use the same origin.
    """
    train_end = origin + pd.Timedelta(days=split_day)
    test_end = last + pd.Timedelta(seconds=1)
    train_alerts = [alert for alert in alerts if alert.timestamp < train_end]
    test_alerts = [alert for alert in alerts if train_end <= alert.timestamp < test_end]
    train_faults = [fault for fault in faults if fault.start < train_end]
    test_faults = [fault for fault in faults if train_end <= fault.start < test_end]
    return train_alerts, test_alerts, train_faults, test_faults, train_end, test_end


def _load(which: str, seed: int):
    if which == "gaia":
        if not (Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()):
            raise SystemExit(
                "GAIA folders are not in this run. Nothing was scored. "
                "On the laptop: uv run --no-sync python run_log_encoder_ablation.py "
                "--data gaia --device cuda --dim 16 --seeds 42,0,1,2,3,7"
            )
        data = load_gaia()
        return data.alerts, data.faults, data.metrics, data.origin, data.last, "gaia"
    folder = Path(tempfile.mkdtemp(prefix="log-encoder-ablation-"))
    generate(folder, seed=seed, days=14)
    alerts, faults = align_alerts_to_faults(load_alerts(folder / "alerts.csv"), load_faults(folder / "faults.csv"))
    origin = min(alert.timestamp for alert in alerts)
    last = max(alert.timestamp for alert in alerts)
    return alerts, faults, None, origin, last, "synthetic"


def _metric_bundle(metrics, faults, config, train_end, test_end):
    """Same metric items as the K=20 block of ``run_classifier_ablation``."""
    if metrics is None or metrics.empty:
        return config, None, None, None
    train_faults = [fault for fault in faults if fault.start < train_end]
    train_jumps = detect_jumps(metrics, config, detect_until=train_end)
    test_jumps = detect_jumps(metrics, config, detect_from=train_end, detect_until=test_end)
    test_gaps = detect_gaps(metrics, config, detect_from=train_end, detect_until=test_end)
    test_leaks = detect_leaks(metrics, config, detect_from=train_end, detect_until=test_end)
    threshold, _by_series = tune_confirm_thresholds(train_jumps, train_faults, config, train_until=train_end)
    tuned = replace(config, confirm_min_rise=threshold)

    def metric_items(tuned=tuned, jumps=test_jumps, leaks=test_leaks, gaps=test_gaps):
        return spike_items(jumps, tuned) + spike_items(leaks, tuned) + spike_items(gaps, tuned)

    return tuned, train_jumps, test_jumps, metric_items


def _seeds(args) -> list[int]:
    if args.seeds:
        return [int(part) for part in args.seeds.split(",") if part.strip()]
    return [args.seed]


def main() -> None:
    parser = argparse.ArgumentParser(description="Contrastive template-encoder ablation.")
    parser.add_argument("--data", choices=("synthetic", "gaia"), default="synthetic")
    parser.add_argument("--dim", type=int, default=4)
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--seeds", default="", help="Comma-separated seeds. Each seed is reported.")
    parser.add_argument("--budget-k", type=int, default=20)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    seeds = _seeds(args)
    alerts, faults, metrics, origin, last, dataset = _load(args.data, seeds[0])
    by_seed = [_one(args, dataset, alerts, faults, metrics, origin, last, seed) for seed in seeds]
    report = dict(by_seed[0])
    report["seeds"] = seeds
    report["by_seed"] = by_seed
    out = _write(report, args.out, dataset)
    for row in by_seed:
        print(
            f"seed={row['seed']} K={args.budget_k} dim={args.dim} decision={row['decision']} "
            f"faults={row.get('test_faults')} "
            f"caught={row.get('baseline_faults_caught')}->{row.get('candidate_faults_caught')} "
            f"false/day={row.get('baseline_false_items_per_day')}->{row.get('candidate_false_items_per_day')} "
            f"delta={row.get('per_fault_catch_delta')}"
        )
    record_run(
        f"log_encoder_{dataset}",
        config=Config(budget_k=args.budget_k),
        outputs=[out],
        metrics={"seeds": seeds, "decisions": [row["decision"] for row in by_seed]},
        note=report["gaia"],
    )


def _one(args, dataset, alerts, faults, metrics, origin, last, seed) -> dict:
    """One seed: refit the encoder and the classifier, then score the full queue."""
    config = Config(budget_k=args.budget_k, random_seed=seed)
    train_alerts, test_alerts, _train_faults, test_faults, train_end, test_end = queue_split(
        alerts, faults, origin, last, config.split_day
    )
    tuned, train_spikes, test_spikes, metric_items = _metric_bundle(metrics, faults, config, train_end, test_end)
    groups = training_groups(train_alerts, faults, train_end)
    report = {
        "dataset": dataset,
        "seed": seed,
        "gaia": "pending: this run did not score GAIA" if dataset != "gaia" else "not scored",
        "log_coverage": LOG_COVERAGE,
        "projection": PROJECTION,
        "dim": args.dim,
        "pages": False,
        "flags_changed": False,
        "headline_k": args.budget_k,
        "test_faults": len(test_faults),
        "window": "last alert",
        "train_groups": len(groups),
        "split": "first week train, later days test, window ending at the last alert",
    }
    try:
        from src.log_encoder_torch import resolve_torch_device, train_state
    except ImportError:
        report["decision"] = "skipped"
        report["reason"] = "torch-missing"
        if dataset == "gaia":
            report["gaia"] = "not scored: torch is not installed"
        return report
    device, fell_back = resolve_torch_device(args.device)
    try:
        state = train_state(groups, dim=args.dim, epochs=args.epochs, seed=seed, device=device)
    except ValueError as exc:
        report["decision"] = "skipped"
        report["reason"] = str(exc)
        report["device"] = device
        report["fell_back"] = fell_back
        return report
    encoder = ContrastiveEncoder(state, dim=args.dim, name=f"contrastive-dim{args.dim}")
    scored = evaluate_template_features(
        train_alerts,
        test_alerts,
        test_faults,
        tuned,
        n_days=n_days_between(train_end, test_end, tuned),
        dataset=dataset,
        encoder=encoder,
        train_spikes=train_spikes,
        test_spikes=test_spikes,
        metric_items=metric_items,
    )
    scored.update(report)
    scored["gaia"] = "scored" if dataset == "gaia" else "pending: this run did not score GAIA"
    scored["device"] = device
    scored["fell_back"] = fell_back
    return scored


def _write(report: dict, out: str | None, dataset: str) -> Path:
    path = Path(out) if out else Path("results") / f"log_encoder_{dataset}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    main()
