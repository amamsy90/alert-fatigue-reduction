"""Read-only paired bootstrap on the frozen day-12 snapshot.

Reuses homelab.evaluate (load_tables, _population, _bounds, lab_config,
BOOTSTRAP_DRAWS=4000, BOOTSTRAP_SEED=42) and the app's run_window,
caught_fault_ids, score_queue and bootstrap_mean_ci, exactly the path the
LOCO check and src.model_eval._decision use: per-fault delta =
caught(candidate) - caught(baseline), KEEP iff 95% interval low > 0.
"""
import json, sys
from dataclasses import replace
from pathlib import Path
import pandas as pd
from homelab import evaluate as ev
from homelab.companies import load_companies

SNAP = Path(sys.argv[1]); OUT = Path(sys.argv[2])
CAND = "p3_ranker"; BASES = ["proposed", "jumps_tuned", "static_p99"]; BUDGETS = [5, 10, 20, 50]
app = ev._imports()
alerts, metrics, faults = ev.load_tables(SNAP / "data" / "collected", SNAP / "data" / "faults.csv")
ids = [c.id for c in load_companies(None) if c.enabled]
present = sorted(set(faults["company"].astype(str)) & set(ids)) if ids else sorted(set(faults["company"].astype(str)))
config = ev.lab_config(7)
results = {"snapshot": str(SNAP), "split_day": 7, "bootstrap_draws": ev.BOOTSTRAP_DRAWS,
           "bootstrap_seed": ev.BOOTSTRAP_SEED, "rule": "KEEP iff 95% bootstrap interval low > 0 on per-fault catch delta (candidate - baseline)",
           "candidate": CAND, "populations": {}}
rows_out = []
for pop in [None] + [c for c in present if c != "cluster"]:
    name = pop or "pooled"
    frame, a, f = ev._population(alerts, metrics, faults, pop)
    origin, last = ev._bounds(frame, a, f)
    train_end = origin + pd.Timedelta(days=config.split_day); test_end = last + pd.Timedelta(seconds=1)
    data = app["GaiaData"](alerts=a, faults=f, metrics=frame, origin=origin, last=last)
    entry = {"train_end": str(train_end), "test_end": str(test_end), "budgets": {}}
    for k in BUDGETS:
        cfg = replace(config, budget_k=k)
        try:
            run = app["run_window"](data, train_end, test_end, cfg)
        except ValueError as exc:
            entry["status"] = f"not scored: {exc}"; break
        tf = run.test_faults
        def stats(q):
            row = app["score_queue"](q, run.shown[q], tf, run.n_days, cfg)
            ids_ = app["caught_fault_ids"](run.shown[q], tf)
            return row, ids_
        crow, cids = stats(CAND)
        kb = {"test_faults": len(tf), "n_days": run.n_days, "comparisons": {}}
        for b in BASES:
            brow, bids = stats(b)
            delta = [float(x.fault_id in cids) - float(x.fault_id in bids) for x in tf]
            iv = app["bootstrap_mean_ci"](delta, n_draw=ev.BOOTSTRAP_DRAWS, seed=ev.BOOTSTRAP_SEED)
            dec = "KEEP" if iv.low > 0 else "DROP"
            rec = {"candidate_caught": len(cids), "baseline_caught": len(bids),
                   "candidate_caught_score_queue": crow["faults_caught"], "baseline_caught_score_queue": brow["faults_caught"],
                   "only_candidate": int(sum(d > 0 for d in delta)), "only_baseline": int(sum(d < 0 for d in delta)),
                   "diff": len(cids) - len(bids), "mean": iv.mean, "low": iv.low, "high": iv.high, "n": iv.n, "decision": dec,
                   "candidate_precision": crow["precision"], "candidate_false_per_day": crow["false_items_per_day"],
                   "baseline_precision": brow["precision"], "baseline_false_per_day": brow["false_items_per_day"]}
            kb["comparisons"][b] = rec
            rows_out.append({"population": name, "k": k, "baseline": b, **rec})
        entry["budgets"][k] = kb
        print(name, k, {b: (r["candidate_caught"], r["baseline_caught"], round(r["low"],4), round(r["high"],4), r["decision"]) for b, r in kb["comparisons"].items()}, flush=True)
    results["populations"][name] = entry
(OUT / "pooled_bootstrap.json").write_text(json.dumps(results, indent=2) + "\n")
pd.DataFrame(rows_out).to_csv(OUT / "pooled_bootstrap.csv", index=False)
print("wrote", OUT)
