"""Trace one real episode through the pipeline (pooled, K=20, day-12 snapshot). Read-only."""
import json, sys
from dataclasses import replace
from pathlib import Path
import pandas as pd
from homelab import evaluate as ev
from src.classifier import AlertClassifier
from src.policy import tune_threshold, spike_review_decisions, bayes_threshold
from src.features import build_feature_frame
from src.memory_spikes import detect_jumps
from src.baseline import baseline_decisions
from src.scoring import faults_by_service, spike_hits
SNAP = Path(sys.argv[1]); OUT = Path(sys.argv[2])
app = ev._imports()
alerts, metrics, faults = ev.load_tables(SNAP / "data" / "collected", SNAP / "data" / "faults.csv")
cfg = replace(ev.lab_config(7), budget_k=20)
frame, a, f = ev._population(alerts, metrics, faults, None)
origin, last = ev._bounds(frame, a, f)
train_end = origin + pd.Timedelta(days=7); test_end = last + pd.Timedelta(seconds=1)
data = app["GaiaData"](alerts=a, faults=f, metrics=frame, origin=origin, last=last)
run = app["run_window"](data, train_end, test_end, cfg)
test_alerts = [x for x in a if train_end <= x.timestamp < test_end]
train_alerts = [x for x in a if x.timestamp < train_end]
train_jumps = detect_jumps(frame, cfg, detect_until=train_end)
test_jumps = run.test_jumps
model = AlertClassifier(cfg).fit(train_alerts, train_jumps)
thr = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_jumps), cfg)
proba = model.predict_proba(test_alerts, test_jumps)
feat = build_feature_frame(test_alerts, cfg, test_jumps)
dec = spike_review_decisions(test_alerts, proba, cfg, thr, bayes_threshold(cfg), [bool(v) for v in feat["during_memory_spike"]])
base = baseline_decisions(test_alerts, cfg)
fmap = {x.fault_id: x for x in f}
shown_ids = {it.alert.alert_id: it for it in run.shown["p3_ranker"] if it.kind == "alert"}
out = {"alert_threshold": float(thr), "bayes_threshold": float(bayes_threshold(cfg)), "examples": []}
for i, al in enumerate(test_alerts):
    if al.alert_id in shown_ids and al.fault_id:
        fl = fmap[al.fault_id]
        out["examples"].append({"alert_id": al.alert_id, "timestamp": str(al.timestamp), "source": al.source, "alertname": al.alert_type,
            "severity": al.severity, "message": al.message, "fault_id": al.fault_id, "fault_start": str(fl.start), "fault_end": str(fl.end),
            "fault_component": fl.component, "classifier_proba": float(proba[i]), "ranker_decision": dec[i].action.value, "ranker_reason": dec[i].reason,
            "severity_baseline_decision": base[i].action.value, "severity_baseline_reason": base[i].reason,
            "attached_to_jump": shown_ids[al.alert_id].attached,
            "features": {k: (float(v) if isinstance(v, (int, float)) else str(v)) for k, v in feat.iloc[i].to_dict().items()}})
by_service = faults_by_service(run.test_faults)
jumps = []
for it in run.shown["p3_ranker"]:
    if it.kind != "alert":
        hits = [x.fault_id for x in spike_hits(it.spike, by_service)]
        if hits:
            s = it.spike
            jumps.append({"kind": it.kind, "service": it.service, "shown_at": str(it.shown_at), "start": str(s.start), "end": str(getattr(s, "end", None)),
                          "rise_at_confirm": float(s.rise_at_confirm), "faults": hits, "fault_windows": [(str(fmap[h].start), str(fmap[h].end), fmap[h].description) for h in hits]})
out["ranker_shown_alerts_total"] = len(shown_ids)
out["ranker_metric_items_catching"] = len(jumps)
out["jump_examples"] = jumps[:5]
(OUT / "worked_example.json").write_text(json.dumps(out, indent=2, default=str) + "\n")
print(json.dumps({**out, "examples": out["examples"][:3]}, indent=1, default=str))
