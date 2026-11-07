"""Accelerated replay of a real alert stream through the whole product loop.

Real GAIA alerts (the committed 10-day sample) are fed to the running app one day at a
time, exactly as Alertmanager would: ingest, a scoring job, an engineer marking the items
shown as Real or Noise, then the scheduled retrain. Each simulated day exercises the
pieces added for production: input drift, the promotion gate, the shadow challenger,
scheduled retraining and the read-back check. Time is accelerated (a day is seconds), so
this is a replay, not a pilot: the engineer is simulated from the ground truth.

    python run_live_replay.py                          # GAIA sample, 90% accurate engineer, 20 marks a day
    python run_live_replay.py --dataset rcaeval:RE2:tt  # RCAEval RE2 Train Ticket, one recording a day
    python run_live_replay.py --accuracy 1.0 --marks-per-day 40

What it measures, per day and in total:
  * precision from marks: what the monitor sees (marked items the engineer called Real)
  * true precision of what was shown (against the injected faults, which the app never sees)
  * input-drift verdicts, retrain outcomes, model versions, challenger tallies

Only the log-alert path runs (no metric series are replayed), so the metric detector is not
part of this check; it is evaluated in run_queue_eval.py. The RCAEval data is not in the repository
(tools/fetch_rcaeval_re23.py). Writes results/live_replay_<dataset>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
from pathlib import Path

import pandas as pd

from src.gaia_loader import load_run_folder

SAMPLE = Path("examples/gaia_sample")
BATCH = 300  # alerts per ingest call (INGEST_MAX_ALERTS is 400)


def _rows(alerts) -> list[dict]:
    return [{"timestamp": a.timestamp.isoformat(), "source": a.source, "severity": a.severity,
             "message": a.message, "alert_type": a.alert_type} for a in alerts]


def stream(dataset: str) -> tuple[dict[str, list], dict]:
    """Alerts grouped by simulated day, and the ground truth the engineer is simulated from."""
    if dataset == "gaia":
        alerts, _faults = load_run_folder(SAMPLE / "run")
        truth = {(a.source, a.timestamp.value): a.fault_id is not None for a in alerts}
        by_day: dict[str, list] = {}
        for a in sorted(alerts, key=lambda x: x.timestamp):
            by_day.setdefault(a.timestamp.strftime("%Y-%m-%d"), []).append(a)
        return by_day, truth
    kind, suite, system = dataset.split(":")
    if kind != "rcaeval":
        raise ValueError(f"unknown dataset {dataset}")
    from run_rcaeval_logs import case_alerts, load_cases
    from src.ingestion import align_alerts_to_faults

    by_day, truth = {}, {}
    for case in load_cases(suite.upper(), system):  # one recording a day, as in run_rcaeval_logs.py
        alerts, _ = align_alerts_to_faults(case_alerts(case), [case.as_fault()])
        if not alerts:
            continue
        by_day[str(case.case)] = alerts
        truth.update({(a.source, a.timestamp.value): a.fault_id is not None for a in alerts})
    return by_day, truth


def replay(dataset: str = "gaia", accuracy: float = 0.9, marks_per_day: int = 20, seed: int = 0,
           budget_k: int = 20) -> dict:
    rng = random.Random(seed)
    by_day, truth = stream(dataset)

    with tempfile.TemporaryDirectory() as tmp:
        for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH", "AUTH_DB"):
            os.environ[name] = str(Path(tmp) / name.lower())
        for name in ("APP_PASSWORD", "ALLOW_OPEN_ACCESS", "INGEST_TOKEN"):
            os.environ.pop(name, None)
        os.environ["SHADOW_MODE"] = "1"
        from fastapi.testclient import TestClient

        import product.app as app_mod
        from product.model_store import load_registry
        from product.retrain import run_retrain
        from product.store import QueueStore

        store = QueueStore(Path(tmp) / "queue.db")
        app_mod.use_store(store)
        client = TestClient(app_mod.app)
        start = pd.Timestamp.now("UTC")
        days = []
        for index, (day, todays) in enumerate(by_day.items()):
            for i in range(0, len(todays), BATCH):
                client.post("/ingest/alerts", json={"alerts": _rows(todays[i:i + BATCH])})
            job = client.post("/jobs/run", json={"mode": "ingest", "budget_k": budget_k}).json()
            meta = job.get("meta", {})
            queue = client.get("/queue", params={"job_id": job["job_id"], "limit": 2000}).json()
            shown = [r for r in queue["items"] if r["action"] in ("escalate", "review")]
            real_shown = sum(truth.get((r["source"], pd.Timestamp(r["timestamp"]).value), False) for r in shown)
            marked = 0
            for row in shown[:marks_per_day]:
                is_real = truth.get((row["source"], pd.Timestamp(row["timestamp"]).value), False)
                if rng.random() > accuracy:
                    is_real = not is_real
                client.post("/feedback", json={"kind": "alert", "id": row["id"],
                                               "verdict": "real" if is_real else "noise"})
                marked += 1
            report = run_retrain(store, trigger="replay", now=start + pd.Timedelta(days=index + 1))
            summary = store.feedback_summary()
            drift = meta.get("drift") or {}
            days.append({
                "day": day, "alerts": len(todays), "shown": len(shown), "true_precision_shown":
                    round(real_shown / len(shown), 4) if shown else None,
                "marked": marked, "marks_real": summary["real"], "marks_noise": summary["noise"],
                "precision_from_marks":
                    round(summary["real"] / (summary["real"] + summary["noise"]), 4)
                    if summary["real"] + summary["noise"] else None,
                "model_used": meta.get("model_used"),
                "drift_status": drift.get("status"), "drift_reasons": drift.get("reasons", []),
                "challenger": (meta.get("challenger") or {}).get("recommendation"),
                "retrain": {"ran": report["ran"], "reason": report.get("reason"),
                            "version": report.get("version"), "promoted": report.get("promoted"),
                            "rolled_back": report.get("rolled_back")},
            })
        registry = load_registry()
    ran = [d["retrain"] for d in days if d["retrain"]["ran"]]
    last = days[-1]
    return {
        "settings": {"accuracy": accuracy, "marks_per_day": marks_per_day, "seed": seed, "budget_k": budget_k,
                     "dataset": dataset},
        "summary": {
            "days": len(days), "alerts": sum(d["alerts"] for d in days),
            "marks": last["marks_real"] + last["marks_noise"], "real_marks": last["marks_real"],
            "precision_from_marks": last["precision_from_marks"],
            "retrains_run": len(ran), "retrains_promoted": sum(1 for r in ran if r["promoted"]),
            "retrains_refused": sum(1 for r in ran if not r["promoted"]),
            "retrains_rolled_back": sum(1 for r in ran if r["rolled_back"]),
            "drift_warn_days": sum(1 for d in days if d["drift_status"] == "warn"),
            "drift_ok_days": sum(1 for d in days if d["drift_status"] == "ok"),
            "model_versions": len(registry["versions"]), "active": registry["active"],
        },
        "days": days,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="gaia", help="gaia (committed sample) or rcaeval:RE2:tt")
    parser.add_argument("--accuracy", type=float, default=0.9, help="share of marks that match the truth")
    parser.add_argument("--marks-per-day", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    result = replay(args.dataset, args.accuracy, args.marks_per_day, args.seed)
    out = Path("results") / f"live_replay_{args.dataset.replace(chr(58), chr(95)).lower()}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for d in result["days"]:
        print(f"{d['day']} alerts {d['alerts']:4d} shown {d['shown']:3d} true-prec {d['true_precision_shown']} "
              f"marks-prec {d['precision_from_marks']} drift {d['drift_status']} model {d['model_used']} "
              f"retrain {d['retrain']['ran']} {d['retrain']['reason'] or ''}"[:200])
    print(json.dumps(result["summary"], indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
