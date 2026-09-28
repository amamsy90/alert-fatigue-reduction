"""Score every on-call queue on GAIA at each daily budget K (the headline table).

Train on week 1, score the next 55 days. All systems use the same causal
scorer (src/queue.py): items appear when a live system could know them, and
each day holds at most K, first come first served.

  logs_severity    log severity + exact duplicates (the usual rule; blind to metrics)
  static_p99       memory > week-1 p99            (static Prometheus rule)
  delta_5m         delta(mem[5m]) > 0.5 GB        (rate Prometheus rule) <- fair baseline
  jumps_hindsight  OLD headline: day's jumps ranked by final peak (NOT causal)
  jumps_online     jump rule, first come
  jumps_tuned      jump rule, first come, rise at confirmation >= week-1 threshold
  proposed         jumps_tuned + monitoring-gap items + log alerts (<= 25% of K)

Usage:
    python run_queue_eval.py
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.config import Config
from src.experiment import MISS_CAUSES, load_gaia, miss_causes, run_window, score_window
from src.tracking import GAIA_INPUTS, record_run


def main() -> None:
    config = Config()
    data = load_gaia()
    split_at = data.origin + pd.Timedelta(days=config.split_day)
    end = data.last + pd.Timedelta(seconds=1)
    rows, misses = [], []
    for k in config.budget_sweep:
        cfg = replace(config, budget_k=k)
        run = run_window(data, split_at, end, cfg)
        if k == config.budget_sweep[0]:
            thr = run.confirm_threshold
            print(
                f"Test: {len(run.test_faults)} faults over {run.n_days} days, "
                f"{len(run.test_jumps)} jumps, {len(run.test_gaps)} gap items. "
                f"Week-1 confirm threshold {thr / 1e9 if thr else 0:.2f} GB."
            )
        for name in ("proposed", "jumps_tuned", "p3_per_series"):
            counts = miss_causes(run, data, cfg, name)
            misses.append({"queue": name, "budget_k": k, **counts})
            print(f"K={k:2d}  misses {name:12s} " + "  ".join(f"{c} {counts[c]}" for c in MISS_CAUSES))
        for row in score_window(run, cfg):
            rows.append(row)
            print(
                f"K={k:2d}  {row['queue']:16s} caught {row['faults_caught']:4d}/{row['faults']} "
                f"({row['detection_rate']:.1%}, {row['share_of_ceiling']:.0%} of ceiling)  "
                f"items/day {row['items_per_day']:5.1f}  false/day {row['false_items_per_day']:4.1f}  "
                f"precision {row['precision']:.0%}  "
                f"confirm {row['confirm_minutes_median']} min"
            )
    out = Path("results") / "queue_eval_gaia.csv"
    out.parent.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(misses).to_csv(out.parent / "miss_causes_gaia.csv", index=False)
    print(f"Results written to {out} and {out.parent / 'miss_causes_gaia.csv'}")
    k20 = {r["queue"]: r for r in rows if r["budget_k"] == 20}
    record_run("queue_eval_gaia", config=config, data=GAIA_INPUTS,
               outputs=[out, out.parent / "miss_causes_gaia.csv"],
               metrics={f"K20.{q}": k20[q]["detection_rate"] for q in ("delta_5m", "jumps_tuned", "proposed") if q in k20})


if __name__ == "__main__":
    main()
