"""Evaluate the generic queue on NAB (non-microservice public data).

Needs `python ../tools/fetch_nab.py` first. For each real NAB category it runs
src/generic.evaluate (tune on the first half of every series, score the second
half) and writes results/nab_eval.csv plus results/nab_settings.csv.

Usage:
    python run_nab.py [train_fraction]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from src.generic import evaluate
from src.tracking import record_run

ROOT = Path("data/nab")
BUDGETS = (1, 2, 5)


def main() -> None:
    train_fraction = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
    rows, settings = [], []
    for folder in sorted((p for p in ROOT.iterdir() if p.is_dir()), key=lambda p: p.name.lower())  # same order on Windows and Linux:
        report = evaluate(folder / "metrics.csv", folder / "incidents.csv", budgets=BUDGETS,
                          train_fraction=train_fraction)
        chosen = report.settings["chosen"]
        s = report.summary
        print(f"\n{folder.name}: {s['series']} series, {s['test_incidents']} test incidents over "
              f"{s['test_days']} days ({s['train_incidents']} to train); chosen detector: {chosen}")
        for row in report.table:
            row.update(dataset=folder.name, chance=report.checks.get("chance_detection"))
            rows.append(row)
            if row["budget_k"] == 2:
                print(f"  K=2 {row['queue']:12s} caught {row['faults_caught']:2d}/{row['faults']:2d} "
                      f"({row['detection_rate']:.0%})  items/day {row['items_per_day']:.2f}  "
                      f"false/day {row['false_items_per_day']:.2f}")
        print(f"  chance {report.checks.get('chance_detection', 0):.0%}  misses {report.misses}")
        for kind in ("jump", "delta", "static"):
            settings.append({"dataset": folder.name, "kind": kind, "chosen": kind == chosen,
                             **{k: v for k, v in report.settings[kind].items()}})
    out = Path("results")
    pd.DataFrame(rows).to_csv(out / "nab_eval.csv", index=False)
    pd.DataFrame(settings).to_csv(out / "nab_settings.csv", index=False)
    frame = pd.DataFrame(rows)
    k2 = frame[frame["budget_k"] == 2].pivot(index="dataset", columns="queue", values="detection_rate")
    print("\nK=2 detection by dataset:\n" + k2.round(3).to_string())
    print(f"\nWrote {out / 'nab_eval.csv'} and {out / 'nab_settings.csv'}")
    mine = frame[(frame["budget_k"] == 2) & (frame["queue"] == "your_queue")]
    record_run("nab", config={"budgets": BUDGETS, "train_fraction": train_fraction}, data=[ROOT],
               outputs=[out / "nab_eval.csv", out / "nab_settings.csv"],
               metrics={f"K2.{d}": float(r) for d, r in zip(mine["dataset"], mine["detection_rate"])})


if __name__ == "__main__":
    main()
