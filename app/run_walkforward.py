"""Walk-forward: retrain on everything before each later week, then score that week.

Per fold, everything learned is re-learned from the past only: the classifier,
its cost threshold, and the jump confirmation threshold. Every system is scored
with the same causal queue. The fair comparison is `proposed` vs `delta_5m`
(a plain Prometheus rate rule), not vs the log-severity rule (always 0% here).

Usage:
    python run_walkforward.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.config import Config
from src.experiment import faults_with_metric_data, load_gaia, run_window, score_window
from src.tracking import GAIA_INPUTS, record_run


def main() -> None:
    config = Config(budget_k=20)
    data = load_gaia()
    end = data.last + pd.Timedelta(seconds=1)
    test_start = data.origin + pd.Timedelta(days=config.split_day)
    rows, fold = [], 0
    while test_start < end:
        test_end = min(test_start + pd.Timedelta(days=7), end)
        if sum(1 for f in data.faults if test_start <= f.start < test_end) < 5:
            break
        run = run_window(data, test_start, test_end, config)
        fold += 1
        with_data = faults_with_metric_data(run.test_faults, data.metrics)
        for row in score_window(run, config):
            row.update(
                fold=fold,
                train_until=test_start.date().isoformat(),
                test_end=test_end.date().isoformat(),
                faults_with_metric_data=round(len(with_data) / len(run.test_faults), 4),
            )
            rows.append(row)
        by = {r["queue"]: r for r in rows if r["fold"] == fold}
        print(
            f"Fold {fold} {test_start.date()}–{test_end.date()}  faults {by['proposed']['faults']}  "
            f"with data {len(with_data) / len(run.test_faults):.0%}  "
            f"delta_5m {by['delta_5m']['detection_rate']:.1%}  "
            f"jumps_tuned {by['jumps_tuned']['detection_rate']:.1%}  "
            f"proposed {by['proposed']['detection_rate']:.1%}  "
            f"(thr {run.confirm_threshold / 1e9 if run.confirm_threshold else 0:.2f} GB)"
        )
        test_start = test_end

    out = Path("results") / "walkforward_gaia.csv"
    out.parent.mkdir(exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(out, index=False)
    means = frame.groupby("queue")["detection_rate"].mean()
    print("Mean over folds: " + ", ".join(f"{q} {means[q]:.1%}" for q in means.index))
    print(f"Wrote {out}")
    record_run("walkforward_gaia", config=Config(), data=GAIA_INPUTS, outputs=[out],
               metrics={f"mean.{q}": float(means[q]) for q in ("delta_5m", "jumps_tuned", "proposed") if q in means})


if __name__ == "__main__":
    main()
