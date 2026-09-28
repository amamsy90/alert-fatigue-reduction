"""Stretch runner: ask a person when the score is uncertain.

Confident faults keep the daily budget first, so recall cannot fall below the
M4 policy as long as those faults fit in K. The rest of the budget is a review
queue. The written report shows how the mix changes at K = 5, 10, 20, 50.

Usage:
    python -m experiments.legacy_alerts_only.run_stretch --gaia-run data/gaia_raw/run/run
    python -m experiments.legacy_alerts_only.run_stretch --sample
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd

from src.classifier import AlertClassifier
from src.config import Config
from src.gaia_loader import load_run_folder
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.metrics import classification_pr, evaluate_all
from src.policy import bayes_threshold, policy_decisions, review_decisions, tune_threshold
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


def _counts(decisions) -> Counter:
    return Counter(d.action.value for d in decisions)


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
    escalate_at = tune_threshold(train_alerts, model.predict_proba(train_alerts), config)
    review_at = bayes_threshold(config)
    test_proba = model.predict_proba(test_alerts)
    print(f"Escalate at P >= {escalate_at:.3f}. Ask a person when {review_at:.3f} <= P < {escalate_at:.3f}.")

    rows = []
    for k in config.budget_sweep:
        cfg = Config(budget_k=k)
        auto = policy_decisions(test_alerts, test_proba, cfg, escalate_at)
        mixed = review_decisions(test_alerts, test_proba, cfg, escalate_at, review_at)
        summary = evaluate_all("review", mixed, test_alerts, test_faults, miss_cost=cfg.miss_cost)
        counts = _counts(mixed)
        auto_recall = classification_pr(auto, test_alerts)["recall"]
        held = summary["recall"] + 1e-12 >= auto_recall
        summary.update(
            {
                "budget_k": k,
                "escalate": counts["escalate"],
                "review": counts["review"],
                "suppress": counts["suppress"],
                "auto_recall": round(auto_recall, 4),
                "recall_held": held,
            }
        )
        rows.append(summary)
        print(
            f"K={k:2d}  escalate={counts['escalate']:4}  review={counts['review']:5}  "
            f"suppress={counts['suppress']:6}  detection={summary['detection_rate']:.3f}  "
            f"recall={summary['recall']:.3f} (auto {auto_recall:.3f})  "
            f"false/day={summary['false_alerts_per_day']}  held={held}"
        )

    frame = pd.DataFrame(rows)
    out = Path("results")
    out.mkdir(exist_ok=True)
    csv_path = out / f"stretch_{tag}.csv"
    frame.to_csv(csv_path, index=False)
    report_path = out / f"budget_sensitivity_{tag}.md"
    report_path.write_text(_report(tag, escalate_at, review_at, frame), encoding="utf-8")
    print(f"\nResults written to {csv_path}")
    print(f"Report written to {report_path}")


def _report(tag: str, escalate_at: float, review_at: float, frame: pd.DataFrame) -> str:
    lines = [
        "# Budget sensitivity",
        "",
        f"Dataset: {tag}. Escalate when P(real) >= {escalate_at:.3f}. "
        f"Ask a person when {review_at:.3f} <= P(real) < {escalate_at:.3f}. "
        "Scores below that floor are suppressed.",
        "",
        "Confident faults take the daily slots first. Uncertain alerts use whatever slots remain. "
        "Recall held means the queue still catches every fault the confident-only policy caught.",
        "",
        "| Budget K | Escalate | Ask a person | Suppress | Faults detected | Recall | False alerts/day | MTTA (min) | Recall held |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in frame.itertuples(index=False):
        mtta = "" if pd.isna(row.mtta_minutes) else f"{row.mtta_minutes:.1f}"
        lines.append(
            f"| {int(row.budget_k)} | {int(row.escalate)} | {int(row.review)} | {int(row.suppress)} | "
            f"{row.detection_rate:.3f} | {row.recall:.3f} | {row.false_alerts_per_day:.2f} | "
            f"{mtta} | {'yes' if row.recall_held else 'no'} |"
        )
    lines += [
        "",
        "Reading the table: a larger budget does not create more confident escalations. "
        "Those are fixed by the week-1 cutoff. Extra slots become questions for a person, "
        "which raises fault detection and also raises the number of false alerts they see.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
