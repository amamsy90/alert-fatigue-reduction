"""Read-only feedback analysis on a day-12 snapshot directory.

Arguments: SNAPSHOT_DIR OUT_DIR. SNAPSHOT_DIR contains data/collected and data/faults.csv.

A1  paired bootstrap p3_ranker vs logs_severity (project queue, unchanged).
A2  Alertmanager-style baseline built from the homelab's real route
    (group_by alertname,company,service; group_wait 10s; group_interval 2m;
    Watchdog|InfoInhibitor -> null receiver), one notification per group flush,
    then the same daily budget K. Two budget orders: severity-then-time (per day,
    needs the whole day, generous) and first-come-first-served (causal).
A3  alert-type / company / service / topology stats.
Method and constants are the ones in day12-pooled-bootstrap/pooled_bootstrap.py.
"""
import json, sys
from collections import defaultdict, Counter
from dataclasses import replace
from pathlib import Path
import pandas as pd
from homelab import evaluate as ev
from homelab.companies import load_companies
from src.config import day_of

SNAP = Path(sys.argv[1]); OUT = Path(sys.argv[2])
CAND = "p3_ranker"; BUDGETS = [5, 10, 20, 50]
GROUP_WAIT = pd.Timedelta(seconds=10); GROUP_INTERVAL = pd.Timedelta(minutes=2)
NULL_ROUTE = {"Watchdog", "InfoInhibitor"}
app = ev._imports()
alerts, metrics, faults = ev.load_tables(SNAP / "data" / "collected", SNAP / "data" / "faults.csv")
ids = [c.id for c in load_companies(None) if c.enabled]
present = sorted(set(faults["company"].astype(str)) & set(ids))
config = ev.lab_config(7)
RANK = config.severity_rank

def am_notifications(test_alerts):
    """One notification per group flush. Returns list of dicts."""
    groups = defaultdict(list)
    for a in sorted(test_alerts, key=lambda x: x.timestamp):
        if a.alert_type in NULL_ROUTE:
            continue
        groups[(a.alert_type, a.source)].append(a)
    notes = []
    for key, items in groups.items():
        cur = None
        for a in items:
            if cur is not None and a.timestamp <= cur["sent_at"]:
                cur["alerts"].append(a); continue
            if cur is None or a.timestamp - cur["sent_at"] >= GROUP_INTERVAL:
                sent = a.timestamp + GROUP_WAIT
            else:
                sent = cur["sent_at"] + GROUP_INTERVAL
            cur = {"group": key, "sent_at": sent, "alerts": [a]}
            notes.append(cur)
    for n in notes:
        n["sev"] = max(RANK.get(a.severity, 0) for a in n["alerts"])
        n["fault_ids"] = {a.fault_id for a in n["alerts"] if a.fault_id}
    return notes

def am_queue(notes, k, order):
    by_day = defaultdict(list)
    for n in notes:
        by_day[day_of(n["sent_at"], config)].append(n)
    shown = []
    for day, ns in by_day.items():
        key = (lambda n: (-n["sev"], n["sent_at"])) if order == "severity" else (lambda n: n["sent_at"])
        shown += sorted(ns, key=key)[:k]
    return shown

def am_stats(shown, known, n_days):
    caught = set()
    useful = false = 0
    for n in shown:
        hit = n["fault_ids"] & known
        caught |= hit
        useful += bool(hit); false += not hit
    return caught, {"items_per_day": round(len(shown) / n_days, 2), "precision": round(useful / (useful + false), 4) if shown else 0.0,
                    "false_items_per_day": round(false / n_days, 2), "faults_caught": len(caught)}

def boot(cids, bids, tf):
    delta = [float(x.fault_id in cids) - float(x.fault_id in bids) for x in tf]
    iv = app["bootstrap_mean_ci"](delta, n_draw=ev.BOOTSTRAP_DRAWS, seed=ev.BOOTSTRAP_SEED)
    return {"candidate_caught": len(cids), "baseline_caught": len(bids), "diff": len(cids) - len(bids),
            "only_candidate": int(sum(d > 0 for d in delta)), "only_baseline": int(sum(d < 0 for d in delta)),
            "mean": iv.mean, "low": iv.low, "high": iv.high, "n": iv.n, "decision": "KEEP" if iv.low > 0 else "DROP"}

res = {"snapshot": str(SNAP), "split_day": 7, "bootstrap_draws": ev.BOOTSTRAP_DRAWS, "bootstrap_seed": ev.BOOTSTRAP_SEED,
       "rule": "KEEP iff 95% bootstrap interval low > 0 on per-fault catch delta (candidate - baseline)",
       "alertmanager_model": {"group_by": ["alertname", "company", "service"], "group_wait_s": 10, "group_interval_s": 120,
                               "repeat_interval": "1h (not modelled: rebuilt episodes carry start times only)",
                               "null_route": sorted(NULL_ROUTE), "severity_rank": RANK},
       "populations": {}}
rows = []
for pop in [None] + [c for c in present if c != "cluster"]:
    name = pop or "pooled"
    frame, a, f = ev._population(alerts, metrics, faults, pop)
    origin, last = ev._bounds(frame, a, f)
    train_end = origin + pd.Timedelta(days=config.split_day); test_end = last + pd.Timedelta(seconds=1)
    data = app["GaiaData"](alerts=a, faults=f, metrics=frame, origin=origin, last=last)
    test_alerts = [x for x in a if train_end <= x.timestamp < test_end]
    entry = {"train_end": str(train_end), "test_end": str(test_end), "test_episodes": len(test_alerts), "budgets": {}}
    for k in BUDGETS:
        cfg = replace(config, budget_k=k)
        try:
            run = app["run_window"](data, train_end, test_end, cfg)
        except ValueError as exc:
            entry["status"] = f"not scored: {exc}"; break
        tf = run.test_faults; known = {x.fault_id for x in tf}
        if k == BUDGETS[0]:
            notes = am_notifications(test_alerts)
            routed = [x for x in test_alerts if x.alert_type not in NULL_ROUTE]
            linked = {x.fault_id for x in test_alerts if x.fault_id in known}
            linked_routed = {x.fault_id for x in routed if x.fault_id in known}
            all_caught, all_stats = am_stats(notes, known, run.n_days)
            entry["alertmanager_unbounded"] = {"episodes": len(test_alerts), "episodes_after_null_route": len(routed),
                "groups": len({n["group"] for n in notes}), "notifications": len(notes),
                "notifications_per_day": round(len(notes) / run.n_days, 2), "n_days": run.n_days,
                "notifications_by_severity": dict(Counter(n["sev"] for n in notes)),
                "test_faults": len(tf), "faults_with_any_aligned_episode": len(linked),
                "faults_with_any_aligned_routed_episode": len(linked_routed), **{"unbounded_" + kk: v for kk, v in all_stats.items()}}
        crow = app["score_queue"](CAND, run.shown[CAND], tf, run.n_days, cfg)
        cids = app["caught_fault_ids"](run.shown[CAND], tf)
        lrow = app["score_queue"]("logs_severity", run.shown["logs_severity"], tf, run.n_days, cfg)
        lids = app["caught_fault_ids"](run.shown["logs_severity"], tf)
        kb = {"test_faults": len(tf), "n_days": run.n_days,
              "p3_ranker": {kk: crow[kk] for kk in ("faults_caught", "items_per_day", "precision", "false_items_per_day", "attached_alerts")},
              "logs_severity": {kk: lrow[kk] for kk in ("faults_caught", "items_per_day", "precision", "false_items_per_day")},
              "comparisons": {}}
        r = boot(cids, lids, tf); kb["comparisons"]["logs_severity"] = r
        rows.append({"population": name, "k": k, "baseline": "logs_severity", **r,
                     "cand_precision": crow["precision"], "cand_false_per_day": crow["false_items_per_day"], "cand_items_per_day": crow["items_per_day"],
                     "base_precision": lrow["precision"], "base_false_per_day": lrow["false_items_per_day"], "base_items_per_day": lrow["items_per_day"]})
        for order in ("severity", "fcfs"):
            bname = f"alertmanager_{order}"
            shown = am_queue(notes, k, order)
            bids, st = am_stats(shown, known, run.n_days)
            r = boot(cids, bids, tf); kb[bname] = st; kb["comparisons"][bname] = r
            rows.append({"population": name, "k": k, "baseline": bname, **r,
                         "cand_precision": crow["precision"], "cand_false_per_day": crow["false_items_per_day"], "cand_items_per_day": crow["items_per_day"],
                         "base_precision": st["precision"], "base_false_per_day": st["false_items_per_day"], "base_items_per_day": st["items_per_day"]})
        entry["budgets"][k] = kb
        print(name, k, {b: (v["candidate_caught"], v["baseline_caught"], round(v["low"], 4), round(v["high"], 4), v["decision"]) for b, v in kb["comparisons"].items()}, flush=True)
    res["populations"][name] = entry
(OUT / "feedback_bootstrap.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
pd.DataFrame(rows).to_csv(OUT / "feedback_bootstrap.csv", index=False)
print(json.dumps({p: v.get("alertmanager_unbounded") for p, v in res["populations"].items()}, indent=1, default=str))
print("wrote", OUT)
