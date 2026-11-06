"""Compare the jump detector with the Prometheus rules and three textbook detectors on GAIA.

Same split, same causal queue and same scorer as run_queue_eval.py. Adds the
z-score, EWMA and isolation-forest detectors of src/baselines_anomaly.py and a 95%
bootstrap interval over the 1,223 test faults, including the paired difference
between the learned-threshold detector and every other system.

    python run_baseline_compare.py

Writes results/baseline_compare_gaia.csv.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.baselines_anomaly import detect_ewma, detect_iforest, detect_zscore
from src.config import Config
from src.entities import Fault
from src.eval_stats import bootstrap_mean_ci
from src.experiment import load_gaia, run_window
from src.queue import QueueItem, build_queue, score_queue, spike_items
from src.scoring import faults_by_service, spike_hits
from src.tracking import GAIA_INPUTS, record_run

BUDGETS = (10, 20, 50)
REFERENCE = "jumps_tuned"
SYSTEMS = ("static_p99", "delta_5m", "zscore", "ewma", "iforest", REFERENCE)


def caught_ids(shown: list[QueueItem], faults: list[Fault]) -> set[str]:
    """Fault ids caught by a shown jump or rule item on the fault's service."""
    by_service = faults_by_service(faults)
    return {f.fault_id for item in shown if item.kind in ("jump", "leak", "rule")
            for f in spike_hits(item.spike, by_service)}


def paired_interval(reference: list[float], other: list[float]) -> tuple[float, float, float]:
    ci = bootstrap_mean_ci([a - b for a, b in zip(reference, other)])
    return round(ci.mean, 4), round(ci.low, 4), round(ci.high, 4)


def main() -> None:
    config = Config()
    data = load_gaia()
    split_at = data.origin + pd.Timedelta(days=config.split_day)
    end = data.last + pd.Timedelta(seconds=1)
    rows = []
    for k in BUDGETS:
        cfg = replace(config, budget_k=k)
        run = run_window(data, split_at, end, cfg)
        shown = dict(run.shown)
        extra = {
            "zscore": detect_zscore(data.metrics, cfg, detect_from=split_at, detect_until=end),
            "ewma": detect_ewma(data.metrics, cfg, detect_from=split_at, detect_until=end),
            "iforest": detect_iforest(data.metrics, cfg, fit_until=split_at, detect_from=split_at,
                                      detect_until=end),
        }
        for name, spikes in extra.items():
            shown[name] = build_queue(spike_items(spikes, cfg), cfg)
        faults = run.test_faults
        hits = {name: caught_ids(shown[name], faults) for name in SYSTEMS}
        vector = {name: [float(f.fault_id in hits[name]) for f in faults] for name in SYSTEMS}
        for name in SYSTEMS:
            row = score_queue(name, shown[name], faults, run.n_days, cfg)
            ci = bootstrap_mean_ci(vector[name])
            diff, low, high = (0.0, 0.0, 0.0) if name == REFERENCE else paired_interval(
                vector[REFERENCE], vector[name])
            row.update({"ci_low": round(ci.low, 4), "ci_high": round(ci.high, 4),
                        f"{REFERENCE}_minus_this": diff, "diff_low": low, "diff_high": high})
            rows.append(row)
            print(f"K={k:2d} {name:11s} caught {row['faults_caught']:4d}/{row['faults']} "
                  f"({row['detection_rate']:.1%}, CI {ci.low:.1%}-{ci.high:.1%})  "
                  f"false/day {row['false_items_per_day']:5.2f}  "
                  f"{REFERENCE} minus this {diff:+.1%} ({low:+.1%} to {high:+.1%})")
    out = Path("results") / "baseline_compare_gaia.csv"
    out.parent.mkdir(exist_ok=True)
    front = ["queue", "budget_k", "faults", "faults_caught", "detection_rate", "ci_low", "ci_high",
             "items_per_day", "false_items_per_day", "precision"]
    frame = pd.DataFrame(rows)
    frame[front + [c for c in frame.columns if c not in front]].to_csv(out, index=False)
    print(f"Results written to {out}")
    k20 = frame[frame["budget_k"] == 20].set_index("queue")["detection_rate"]
    record_run("baseline_compare_gaia", config=config, data=GAIA_INPUTS, outputs=[out],
               metrics={f"K20.{name}": float(k20[name]) for name in SYSTEMS})


if __name__ == "__main__":
    main()
