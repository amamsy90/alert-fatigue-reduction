"""A3: alert types, companies, services, fault->episode fan-out on the frozen day-12 snapshot."""
import json, sys
from collections import Counter, defaultdict
from pathlib import Path
import pandas as pd
from homelab import evaluate as ev

SNAP = Path(sys.argv[1]); OUT = Path(sys.argv[2])
GRACE = pd.Timedelta(minutes=10)   # episode start within [fault.start, fault.end + 10 min]
BURST = pd.Timedelta(minutes=2)    # same-company episodes chained within 2 min = one burst
CATEGORY = {"HomelabCpuHigh": "cpu", "HomelabMemoryHigh": "memory", "HomelabLatencyP95High": "latency",
            "HomelabErrorRateHigh": "error_rate", "HomelabRestarted": "pod_restart",
            "KubePodCrashLooping": "pod_restart", "KubePodNotReady": "cluster_k8s", "KubeDeploymentRolloutStuck": "cluster_k8s",
            "NodeMemoryMajorPagesFaults": "cluster_node", "InfoInhibitor": "meta (null route)", "Watchdog": "meta (null route)"}
raw = pd.read_csv(SNAP / "data" / "collected" / "alerts.csv")
rebuilt = pd.read_csv(SNAP / "alerts_rebuilt.csv")
fr = pd.read_csv(SNAP / "data" / "faults.csv")
raw["category"] = raw.alert_type.map(CATEGORY).fillna("other")
raw["ts"] = pd.to_datetime(raw.timestamp, utc=True, format="mixed")
out = {"episodes_rebuilt_file": len(rebuilt), "episodes_evaluated_file": len(raw),
       "by_alertname": raw.alert_type.value_counts().to_dict(),
       "by_category": raw.category.value_counts().to_dict(),
       "by_severity": raw.severity.value_counts().to_dict(),
       "by_company": raw.company.value_counts().to_dict(),
       "company_x_category": {c: g.category.value_counts().to_dict() for c, g in raw.groupby("company")},
       "distinct_sources": int(raw.source.nunique()),
       "top_sources": raw.source.value_counts().head(15).to_dict(),
       "severity_x_alertname": {s: g.alert_type.value_counts().to_dict() for s, g in raw.groupby("severity")}}
closed = fr[fr.status == "closed"].copy()
closed["start"] = pd.to_datetime(closed.start, utc=True); closed["end"] = pd.to_datetime(closed.end, utc=True)
out["faults_closed"] = len(closed); out["faults_not_injected"] = int((fr.status != "closed").sum())
out["faults_by_company_type"] = {c: g.fault_type.value_counts().to_dict() for c, g in closed.groupby("company")}
# Strict project alignment (evaluator rule) on the pooled population
_, A, F = ev._population(raw.drop(columns=["category", "ts"]), pd.DataFrame(), fr, None)
strict_linked = Counter(a.fault_id for a in A if a.fault_id)
out["strict_alignment"] = {"episodes_aligned_to_a_fault": int(sum(strict_linked.values())),
                           "episodes_noise": int(sum(1 for a in A if not a.fault_id)),
                           "faults_with_ge1_aligned_episode": len(strict_linked), "faults_total": len(F),
                           "aligned_by_alertname": dict(Counter(a.alert_type for a in A if a.fault_id))}
# Fan-out with a grace window, same company, any service (routed alerts only)
routed = raw[~raw.alert_type.isin(["Watchdog", "InfoInhibitor"])]
fan = []
for f in closed.itertuples():
    w = routed[(routed.company == f.company) & (routed.ts >= f.start) & (routed.ts <= f.end + GRACE)]
    own = w[w.source == f.component]; other = w[w.source != f.component]
    fan.append({"fault_id": f.fault_id, "company": f.company, "fault_type": f.fault_type, "episodes": len(w),
                "services": int(w.source.nunique()), "own_service_episodes": len(own), "other_service_episodes": len(other),
                "other_services": sorted(other.source.unique())})
fan = pd.DataFrame(fan)
fan.to_csv(OUT / "fault_fanout.csv", index=False)
def summ(d):
    return {"faults": len(d), "with_ge1_episode": int((d.episodes > 0).sum()), "with_episodes_on_ge2_services": int((d.services >= 2).sum()),
            "with_episode_on_faulted_service": int((d.own_service_episodes > 0).sum()),
            "with_episode_only_on_other_services": int(((d.own_service_episodes == 0) & (d.other_service_episodes > 0)).sum()),
            "episodes_in_windows": int(d.episodes.sum()), "mean_episodes_when_any": round(float(d[d.episodes > 0].episodes.mean()), 2) if (d.episodes > 0).any() else None}
out["fanout_grace_10min"] = {"pooled": summ(fan), **{c: summ(g) for c, g in fan.groupby("company")},
                             "by_fault_type": {t: summ(g) for t, g in fan.groupby("fault_type")}}
# Which other services co-fire most (proxy for dependency, observed only)
pairs = Counter()
for r in fan.itertuples():
    comp = closed.set_index("fault_id").loc[r.fault_id, "component"]
    for s in r.other_services:
        pairs[f"{comp} -> {s}"] += 1
out["top_cofiring_pairs"] = dict(pairs.most_common(15))
# Episodes in no fault window (noise share), grace rule
inwin = set()
for f in closed.itertuples():
    m = (routed.company == f.company) & (routed.ts >= f.start) & (routed.ts <= f.end + GRACE)
    inwin |= set(routed.index[m])
out["routed_episodes"] = len(routed); out["routed_episodes_in_any_same_company_fault_window_grace"] = len(inwin)
# Fold-down: episodes -> null route -> AM groups (alertname,company,service) flushes -> cross-service bursts
def flushes(df):
    n = 0
    for _, g in df.sort_values("ts").groupby(["alert_type", "source"]):
        sent = None
        for t in g.ts:
            if sent is not None and t <= sent: continue
            sent = t + pd.Timedelta(seconds=10) if sent is None or t - sent >= pd.Timedelta(minutes=2) else sent + pd.Timedelta(minutes=2)
            n += 1
    return n
bursts = 0
for _, g in routed.sort_values("ts").groupby("company"):
    last = None
    for t in g.ts:
        if last is None or t - last > BURST: bursts += 1
        last = t
out["fold_down"] = {"episodes": len(raw), "after_null_route": len(routed), "alertmanager_groups": int(routed.groupby(["alert_type", "source"]).ngroups),
                    "alertmanager_notifications_no_repeat": flushes(routed),
                    "same_company_bursts_2min_chain": bursts}
# Project queue dedup effects at K=20 from the evaluated tables (files, not recomputed)
qe = pd.read_csv(SNAP / "results-day12-rebuilt-ranker" / "pooled" / "queue_eval.csv")
out["queue_eval_pooled"] = qe[qe.queue.isin(["proposed", "p3_grouped", "p3_carryover", "p3_ranker", "logs_severity"])][
    ["queue", "budget_k", "faults_caught", "items_per_day", "attached_alerts", "false_items_per_day", "precision"]].to_dict("records")
(OUT / "topology_stats.json").write_text(json.dumps(out, indent=2, default=str) + "\n")
print(json.dumps(out, indent=1, default=str))
