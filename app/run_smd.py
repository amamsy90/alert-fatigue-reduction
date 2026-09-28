"""Second benchmark: the Server Machine Dataset (SMD) — real server incidents on
CPU, memory, disk, network and TCP metrics, not GAIA's one memory program.

Needs `python ../tools/fetch_smd.py` first (28 machines, one-minute samples,
38 metrics each, labelled incidents with the metrics that moved). Each machine
is one dataset: tuned on the first `train_fraction` of every series, scored on
the rest, K items a day per machine, exactly like the Evaluate page.

Two questions are answered:
  * per metric (the engine's table): of the metric movements engineers flagged,
    how many did the queue show?
  * per incident: an incident counts once and is caught if any of the metrics
    that moved was shown while it lasted.

Usage:
    python run_smd.py [train_fraction]      # default 0.5
Writes results/smd_eval.csv (per machine, per K) and prints the summary.
Each machine takes about 10-40 s; finished machines are cached in
results/smd_cache/ so an interrupted run resumes where it stopped.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.config import Config
from src.generic import (
    _faults_part,
    _spikes_part,
    detect,
    evaluate,
    load_incidents,
    load_metrics,
    setting_from_dict,
    split_points,
)
from src.queue import build_queue, spike_items
from src.tracking import record_run

ROOT = Path("data/smd")
CACHE = Path("results/smd_cache")
BUDGETS = (5, 10, 20)


def incident_level(folder: Path, report, train_fraction: float) -> dict[int, tuple[int, int]]:
    """{K: (incidents caught, incidents)} on the test part with the chosen detector."""
    frame, _ = load_metrics(folder / "metrics.csv")
    cuts = split_points(frame, train_fraction)
    faults, _ = load_incidents(folder / "incidents.csv", 10.0, set(frame["series_id"].astype(str)))
    test = _faults_part(faults, cuts, "test")
    raw = pd.read_csv(folder / "incidents.csv")
    key = {(str(r.series), pd.Timestamp(r.start).isoformat()): r.incident for r in raw.itertuples()}
    groups: dict[str, list] = {}
    for f in test:
        groups.setdefault(key.get((f.component, f.start.isoformat()), f.fault_id), []).append(f)
    chosen = report.settings["chosen"]
    setting = setting_from_dict({k: v for k, v in report.settings[chosen].items() if k != "label"})
    spikes = _spikes_part(detect(frame, setting, cuts), cuts, "test")
    out = {}
    for k in BUDGETS:
        cfg = replace(Config(), budget_k=k)
        shown = [i.payload for i in build_queue(spike_items(spikes, cfg), cfg) if i.kind != "gap"]
        caught = 0
        for members in groups.values():
            if any(s.series_id == f.component and s.start <= f.end and s.end >= f.start
                   for f in members for s in shown):
                caught += 1
        out[k] = (caught, len(groups))
    return out


def main() -> None:
    train_fraction = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
    rows = []
    CACHE.mkdir(parents=True, exist_ok=True)
    for folder in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        cached = CACHE / f"{folder.name}_{train_fraction}.json"
        if cached.exists():
            rows.extend(json.loads(cached.read_text()))
            continue
        report = evaluate(folder / "metrics.csv", folder / "incidents.csv", budgets=BUDGETS,
                          train_fraction=train_fraction)
        s = report.summary
        by_incident = incident_level(folder, report, train_fraction)
        sizes = report.settings["jump"].get("z_by_series") or {}
        print(f"{folder.name}: {s['test_incidents']} metric movements in {by_incident[BUDGETS[0]][1]} incidents "
              f"over {s['test_days']} days ({s['train_incidents']} movements to train); "
              f"chosen {report.settings['chosen']}, per-series sizes {len(sizes)}, "
              f"chance {report.checks.get('chance_detection', 0):.0%}")
        for row in report.table:
            caught, total = by_incident[row["budget_k"]]
            row.update(dataset=folder.name, chance=report.checks.get("chance_detection"),
                       tuned=bool(report.settings[report.settings["chosen"]].get("tuned")),
                       incidents=total,
                       incidents_caught=caught if row["queue"] == "your_queue" else None)
            rows.append(row)
        cached.write_text(json.dumps(rows[-len(report.table):], default=str))
    frame = pd.DataFrame(rows)
    out = Path("results") / "smd_eval.csv"
    frame.to_csv(out, index=False)
    mine = frame[frame["queue"] == "your_queue"]
    record_run("smd", config={"budgets": BUDGETS, "train_fraction": train_fraction}, data=[ROOT], outputs=[out],
               metrics={f"K{k}.incidents": float(g["incidents_caught"].sum() / g["incidents"].sum())
                        for k, g in mine.groupby("budget_k")})
    print("\nPer metric movement, mean detection over machines:")
    print(frame.pivot_table(index="budget_k", columns="queue", values="detection_rate").round(3).to_string())
    yq = frame[frame["queue"] == "your_queue"]
    print("\nYour queue, all machines pooled:")
    for k, part in yq.groupby("budget_k"):
        print(f"  K={k:2d}: metric movements {part['faults_caught'].sum()}/{part['faults'].sum()} "
              f"({part['faults_caught'].sum() / part['faults'].sum():.1%}); "
              f"incidents {int(part['incidents_caught'].sum())}/{int(part['incidents'].sum())} "
              f"({part['incidents_caught'].sum() / part['incidents'].sum():.1%}); "
              f"items/day {part['items_per_day'].mean():.1f}, false/day {part['false_items_per_day'].mean():.1f}; "
              f"chance {part['chance'].mean():.1%}; tuned machines {int(part['tuned'].sum())}/{len(part)}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
