"""Numbers a marker can defend. Reads the walk-forward table; reloads GAIA for
signal counts, per-service rates, chance overlap and confirmation times.

Usage:
    python run_stats.py
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import Config
from src.eval_stats import (
    SIGNALS,
    bootstrap_mean_ci,
    by_service_rows,
    chance_overlap,
    false_jumps_near_other_faults,
    faults_with_jumps,
    sign_test_greater,
    signal_counts,
)
from src.experiment import faults_with_metric_data, load_gaia
from src.memory_spikes import detect_gaps, detect_jumps, faults_in_gaps
from src.queue import oracle_ceiling
from src.tracking import GAIA_INPUTS, record_run


def _fold_rates(walk: pd.DataFrame, queue: str) -> list[float]:
    part = walk.loc[walk["queue"] == queue].sort_values("fold")
    return [float(x) for x in part["detection_rate"]]


def main() -> None:
    config = Config()
    walk = pd.read_csv(Path("results") / "walkforward_gaia.csv")
    proposed = _fold_rates(walk, "proposed")
    tuned = _fold_rates(walk, "jumps_tuned")
    delta = _fold_rates(walk, "delta_5m")
    carry = _fold_rates(walk, "p3_carryover")
    delta_carry = _fold_rates(walk, "delta_5m_carryover")
    carry_vs = sign_test_greater(carry, delta_carry)
    ci = bootstrap_mean_ci(proposed)
    ci_tuned = bootstrap_mean_ci(tuned)
    vs_delta = sign_test_greater(proposed, delta)
    tuned_vs_delta = sign_test_greater(tuned, delta)
    print(f"Walk-forward proposed {ci.mean:.1%} (95% CI {ci.low:.1%}-{ci.high:.1%}); "
          f"jumps_tuned {ci_tuned.mean:.1%}; delta_5m {np.mean(delta):.1%}")
    print(f"proposed > delta_5m in {vs_delta.wins}/{vs_delta.n} non-tied folds (p = {vs_delta.p_value:.4f}); "
          f"jumps_tuned > delta_5m {tuned_vs_delta.wins}/{tuned_vs_delta.n} (p = {tuned_vs_delta.p_value:.4f})")

    print(f"With carry-over (Phase 3): proposed {np.mean(carry):.1%} vs delta_5m {np.mean(delta_carry):.1%}, "
          f"{carry_vs.wins}/{carry_vs.n} non-tied folds (p = {carry_vs.p_value:.4f})")

    data = load_gaia()
    split_at = data.origin + pd.Timedelta(days=config.split_day)
    tags = Counter(f.description for f in data.faults)
    test_alerts = [a for a in data.alerts if a.timestamp >= split_at]
    test_faults = [f for f in data.faults if f.start >= split_at]
    jumps = detect_jumps(data.metrics, config, detect_from=split_at)
    gaps = detect_gaps(data.metrics, config, detect_from=split_at)
    with_data = faults_with_metric_data(test_faults, data.metrics)
    n = len(test_faults)

    signals = signal_counts(test_faults, test_alerts, jumps, with_data)
    print("Test faults by signal:")
    for key in SIGNALS:
        print(f"  {key:15s} {signals[key]:4d}  ({signals[key] / n:.1%})")
    jumped = faults_with_jumps(test_faults, jumps)
    caught_with_data = len(jumped & with_data) / len(with_data) if with_data else 0.0
    in_gap = faults_in_gaps(gaps, test_faults)
    chance = chance_overlap(test_faults, jumps)
    ceiling = oracle_ceiling(test_faults, config)
    near = false_jumps_near_other_faults(data.faults, jumps)
    delays = [
        (j.confirm - f.start).total_seconds() / 60
        for j in jumps
        for f in test_faults
        if f.component == j.service and j.start <= f.end and j.end >= f.start
    ]
    print(f"Jump rule on faults with metric data: {caught_with_data:.1%} ({len(jumped & with_data)}/{len(with_data)})")
    print(f"Faults that started inside a monitoring gap: {len(in_gap)}; gap items: {len(gaps)}")
    print(f"Chance overlap (jumps shifted 1-6 h): {chance[0]:.1%} (range {chance[1]:.1%}-{chance[2]:.1%})")
    print(f"Oracle ceiling at K={config.budget_k}: {ceiling:.1%}")
    print(f"False jumps starting within 10 min of a fault on any service: {near['near_share']:.1%} "
          f"of {int(near['false_jumps'])} (random times: {near['chance_share']:.1%}) — "
          f"{'no sign' if near['near_share'] <= near['chance_share'] * 1.2 else 'a sign'} of faults logged under the wrong service.")
    print(f"Fault start -> jump confirmed: median {np.median(delays):.2f} min, p90 {np.percentile(delays, 90):.2f}")

    rows = by_service_rows(test_faults, test_alerts, jumps)
    print("Test faults by service:")
    for row in rows:
        print(f"  {row['service']:14s} faults {row['faults']:4d}  log {row['log_rate']:.1%}  jump {row['jump_rate']:.1%}")

    weak = walk.loc[walk["queue"] == "proposed"].sort_values("detection_rate").iloc[0]
    print(f"Weakest fold {int(weak['fold'])} ({weak['train_until']} to {weak['test_end']}): "
          f"{weak['detection_rate']:.1%}; faults with metric data {weak['faults_with_metric_data']:.0%}.")

    headline = {
        "folds": ci.n,
        "mean": round(ci.mean, 4),
        "ci_low": round(ci.low, 4),
        "ci_high": round(ci.high, 4),
        "tuned_mean": round(ci_tuned.mean, 4),
        "delta_mean": round(float(np.mean(delta)), 4),
        "wins_vs_delta": vs_delta.wins,
        "sign_n_vs_delta": vs_delta.n,
        "sign_p_vs_delta": vs_delta.p_value,
        "tuned_wins_vs_delta": tuned_vs_delta.wins,
        "tuned_sign_n_vs_delta": tuned_vs_delta.n,
        "tuned_sign_p_vs_delta": tuned_vs_delta.p_value,
        "carry_mean": round(float(np.mean(carry)), 4),
        "delta_carry_mean": round(float(np.mean(delta_carry)), 4),
        "carry_wins_vs_delta": carry_vs.wins,
        "carry_sign_n_vs_delta": carry_vs.n,
        "carry_sign_p_vs_delta": carry_vs.p_value,
        "test_faults": n,
        **{f"signal_{k}": v for k, v in signals.items()},
        "faults_with_metric_data": len(with_data),
        "jump_rate_with_data": round(caught_with_data, 4),
        "faults_in_gaps": len(in_gap),
        "gap_items": len(gaps),
        "chance_overlap": round(chance[0], 4),
        "oracle_ceiling_k20": round(ceiling, 4),
        "false_jumps": int(near["false_jumps"]),
        "false_jumps_near_fault": round(near["near_share"], 4),
        "false_jumps_near_chance": round(near["chance_share"], 4),
        "confirm_minutes_median": round(float(np.median(delays)), 2),
        "confirm_minutes_p90": round(float(np.percentile(delays, 90)), 2),
        "weakest_fold": int(weak["fold"]),
        "weakest_rate": round(float(weak["detection_rate"]), 4),
        "weakest_with_data": float(weak["faults_with_metric_data"]),
        "injection_tags": ";".join(f"{k}:{v}" for k, v in tags.items()),
    }
    out = Path("results")
    pd.DataFrame([headline]).to_csv(out / "stats_gaia.csv", index=False)
    pd.DataFrame(rows).to_csv(out / "stats_by_service.csv", index=False)
    print(f"Wrote {out / 'stats_gaia.csv'} and {out / 'stats_by_service.csv'}")
    record_run("stats_gaia", data=GAIA_INPUTS, outputs=[out / "stats_gaia.csv", out / "stats_by_service.csv"],
               metrics={k: headline[k] for k in ("mean", "tuned_mean", "delta_mean", "wins_vs_delta",
                                                 "tuned_wins_vs_delta", "confirm_minutes_median") if k in headline})


if __name__ == "__main__":
    main()
