"""Walk-forward check: retrain on everything before each later week, then score that week.

Week 1 (days 0-7) is only used to train. Each later 7-day block is scored once,
then added to the training pool. The jump size stays 0.5 GB (chosen on week 1).
The headline split (train week 1, test all later days at once) is still the
main result. This script asks whether that result was a lucky later period.

Usage:
    python run_walkforward.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from run_queue_eval import (
    _alert_items,
    _jump_items,
    _score,
    _shared,
)
from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config
from src.features import build_feature_frame
from src.gaia_loader import load_run_folder
from src.memory_spikes import detect_jumps, load_metric_folder
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold


def _in_window(items, start, end):
    return [x for x in items if start <= _when(x) < end]


def _when(item):
    return getattr(item, "timestamp", None) or item.start


def main() -> None:
    config = Config(budget_k=20)
    alerts, faults = load_run_folder("data/gaia_raw/run/run")
    metrics = load_metric_folder("data/gaia_metrics")
    origin = min(a.timestamp for a in alerts)
    last = max(a.timestamp for a in alerts)
    fold_days = 7
    rows = []
    fold = 0
    test_start = origin + pd.Timedelta(days=config.split_day)
    while test_start < last:
        test_end = min(test_start + pd.Timedelta(days=fold_days), last + pd.Timedelta(seconds=1))
        train_alerts = [a for a in alerts if a.timestamp < test_start]
        test_alerts = _in_window(alerts, test_start, test_end)
        test_faults = _in_window(faults, test_start, test_end)
        if len(test_alerts) < 10 or len(test_faults) < 5:
            break
        if len({a.fault_id is not None for a in train_alerts}) < 2:
            break
        train_jumps = detect_jumps(metrics, config, detect_until=test_start)
        test_jumps = detect_jumps(metrics, config, detect_from=test_start, detect_until=test_end)
        model = AlertClassifier(config).fit(train_alerts, train_jumps)
        threshold = tune_threshold(
            train_alerts, model.predict_proba(train_alerts, train_jumps), config
        )
        proba = model.predict_proba(test_alerts, test_jumps)
        in_jump = [
            bool(v)
            for v in build_feature_frame(test_alerts, config, test_jumps)["during_memory_spike"]
        ]
        n_days = max(1, (test_end.normalize() - test_start.normalize()).days)
        learned = _alert_items(
            test_alerts,
            spike_review_decisions(
                test_alerts, proba, config, threshold, bayes_threshold(config), in_jump
            ),
        )
        shown = _shared(_jump_items(test_jumps), learned, config.budget_k)
        base = _alert_items(test_alerts, baseline_decisions(test_alerts, config))
        row_sys = _score("shared_jumps_1st", config.budget_k, shown, test_faults, n_days)
        row_base = _score("baseline", config.budget_k, base, test_faults, n_days)
        fold += 1
        for row, name in ((row_sys, "shared_jumps_1st"), (row_base, "baseline")):
            row["fold"] = fold
            row["train_until"] = test_start.date().isoformat()
            row["test_end"] = test_end.date().isoformat()
            row["queue"] = name
            row["threshold"] = round(threshold, 3)
            rows.append(row)
        print(
            f"Fold {fold} train < {test_start.date()}  test {test_start.date()}–{test_end.date()}  "
            f"faults {row_sys['faults']}  "
            f"baseline {row_base['detection_rate']:.1%}  "
            f"queue {row_sys['detection_rate']:.1%}  "
            f"false/day {row_sys['false_items_per_day']}"
        )
        test_start = test_end

    out = Path("results") / "walkforward_gaia.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    sys_rows = [r for r in rows if r["queue"] == "shared_jumps_1st"]
    if sys_rows:
        rates = [r["detection_rate"] for r in sys_rows]
        print(
            f"Walk-forward mean detection {sum(rates) / len(rates):.1%} "
            f"(min {min(rates):.1%}, max {max(rates):.1%}) over {len(sys_rows)} folds. "
            f"Wrote {out}"
        )


if __name__ == "__main__":
    main()
