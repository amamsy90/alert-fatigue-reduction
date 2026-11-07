"""Live soak: the real stack, in real time, with faults injected and an engineer marking the queue.

    uv run python run_live_soak.py --minutes 120          # needs Docker; about 2 hours
    uv run python run_live_soak.py --minutes 15 --warmup 300 --fault-every 120 --job-every 60   # a short try

It starts integration/live/docker-compose.yml: the built app image, a real Prometheus that
scrapes a simulated fleet and the app, and a real Alertmanager that posts firing alerts to the
app's webhook. The fleet injects faults (+1 GB for 10 minutes), benign bumps and error bursts
in real time and keeps the ground truth. This runner then does what an on-call team and a
scheduler do: runs an ingest job every few minutes, marks the items shown as Real or Noise
(90% accurate against the truth), calls the retrain job on a schedule, and records what the
monitoring said. It writes results/live_soak.json.

What is real: Prometheus scraping, the Alertmanager webhook, ingest, scoring on live series,
the marks, the scheduled retrain, drift checks, and the app's own /metrics scraped by
Prometheus. What is not: the services are simulated, the faults are injected, and the engineer is
simulated from the truth. It is a soak test, not a production deployment and not a pilot.
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "integration" / "live" / "docker-compose.yml"
APP, FLEET, PROM = "http://127.0.0.1:18090", "http://127.0.0.1:19100", "http://127.0.0.1:19091"
USER, PASSWORD = "oncall", "live-soak-password"
INGEST_TOKEN = "live-soak-token"
OUT = Path("results") / "live_soak.json"


# --- matching the queue to the truth (pure, tested without Docker) ----------------------------


def _t(value) -> float:
    return pd.Timestamp(value).timestamp()


def fault_for_jump(jump: dict, faults: list[dict], pad: float = 60.0) -> str | None:
    """The injected fault a shown jump belongs to: same service, windows overlapping within `pad` seconds."""
    start, end = _t(jump["start"]), _t(jump["end"])
    for f in faults:
        if f["service"] == jump["service"] and start - pad <= f["end"] and end + pad >= f["start"]:
            return f["id"]
    return None


def alert_is_fault(source: str, timestamp, faults: list[dict], pad: float = 120.0) -> bool:
    """An alert is a real fault alert when it fires on the faulty service during the fault."""
    when = _t(timestamp)
    return any(f["service"] == source and f["start"] <= when <= f["end"] + pad for f in faults)


def engineer_verdict(is_real: bool, accuracy: float, rng: random.Random) -> str:
    """What a 90% accurate engineer marks: the truth, flipped with probability 1 - accuracy."""
    if rng.random() > accuracy:
        is_real = not is_real
    return "real" if is_real else "noise"


def summarize(faults: list[dict], seen: dict, now: float, settle: float = 90.0) -> dict:
    """Detection, delay and false items for faults that had ended and settled before `now`.

    `seen` maps (service, start) of a shown jump to {"fault": id or None, "confirm": epoch}.
    """
    judged = [f for f in faults if f["end"] + settle <= now]
    first_confirm: dict[str, float] = {}
    for item in seen.values():
        if item["fault"] and item["confirm"] is not None:
            first_confirm[item["fault"]] = min(item["confirm"], first_confirm.get(item["fault"], 1e18))
    caught = [f for f in judged if f["id"] in first_confirm]
    delays = sorted(first_confirm[f["id"]] - f["start"] for f in caught)
    false_jumps = [k for k, v in seen.items() if v["fault"] is None]
    shown = len(seen)
    return {
        "faults_injected": len(faults), "faults_judged": len(judged), "faults_caught": len(caught),
        "detection_rate": round(len(caught) / len(judged), 4) if judged else None,
        "median_delay_minutes": round(delays[len(delays) // 2] / 60, 2) if delays else None,
        "shown_jumps": shown, "false_jumps": len(false_jumps),
        "true_precision_jumps": round(1 - len(false_jumps) / shown, 4) if shown else None,
    }


# --- talking to the stack ---------------------------------------------------------------------


def call(base: str, method: str, path: str, body: dict | None = None, auth: bool = True, timeout: float = 120.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if path.startswith("/ingest/"):
        request.add_header("Authorization", "Bearer " + INGEST_TOKEN)  # the webhook has its own token
    elif auth:
        request.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode()
            return response.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()
        try:
            return exc.code, json.loads(text)
        except ValueError:
            return exc.code, {"detail": text}
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"detail": str(exc)}


# --- the drift and retrain phase --------------------------------------------------------------
# A two-hour soak posts only a few alerts, too few for the drift check (30 per job, 100 of history).
# This phase posts alert storms to the webhook instead: three of the usual mix, one whose mix and
# wording moved, one more of the usual mix. The alerts are made by this script, not by Prometheus.


def make_storm(kind: str, count: int, end: pd.Timestamp, span_hours: float, tag: str, rng: random.Random):
    """(rows, truth) for one storm. `truth` maps (source, timestamp) to whether it is a real fault."""
    rows, truth = [], {}
    for i in range(count):
        when = end - pd.Timedelta(seconds=rng.uniform(0, span_hours * 3600))
        stamp = when.isoformat()
        if kind == "usual":
            name = rng.choices(["HighMemory", "ErrorBurst", "Watchdog"], weights=[6, 3, 1])[0]
            source = rng.choice([f"svc{n}" for n in range(1, 7)])
            severity = "info" if name == "Watchdog" else "warning"
            real = name == "HighMemory" and rng.random() < 0.5
            message = {"HighMemory": f"container {source} memory above 3.2 GB for 30s",
                       "ErrorBurst": f"{source} error ratio above 0.2 for 20s",
                       "Watchdog": "always firing"}[name]
        else:
            name = rng.choice(["DiskPressure", "NodeNotReady"])
            source = rng.choice([f"node{n}" for n in range(1, 5)])
            severity, real = "critical", True
            message = f"kubelet on {source} reports {name}: /var/lib/docker above 90 percent, pods being evicted"
        rows.append({"alert_id": f"{tag}-{i}", "timestamp": stamp, "source": source, "alert_type": name,
                     "severity": severity, "message": message})
        truth[(source, pd.Timestamp(stamp).value)] = real
    return rows, truth


def run_storm_job(rows: list[dict], truth: dict, accuracy: float, rng: random.Random) -> dict:
    code, body = call(APP, "POST", "/ingest/alerts", {"alerts": rows})
    if code != 200:
        return {"error": f"ingest {code}: {body.get('detail')}"}
    code, job = call(APP, "POST", "/jobs/run", {"mode": "ingest", "budget_k": 50})
    if code != 200:
        return {"error": f"job {code}: {job.get('detail')}"}
    _, queue = call(APP, "GET", f"/queue?job_id={job['job_id']}&limit=2000")
    marked = 0
    for item in queue.get("items", []):
        if item["action"] not in ("escalate", "review"):
            continue
        real = truth.get((item["source"], pd.Timestamp(item["timestamp"]).value), False)
        call(APP, "POST", "/feedback", {"kind": "alert", "id": item["id"],
                                        "verdict": engineer_verdict(real, accuracy, rng)})
        marked += 1
    drift = (job.get("meta") or {}).get("drift") or {}
    return {"alerts": len(rows), "marked": marked, "model_used": (job.get("meta") or {}).get("model_used"),
            "drift": drift.get("status"), "psi_max": drift.get("psi_max"),
            "reasons": drift.get("reasons"), "text_auc": drift.get("text_auc")}


def drift_phase(accuracy: float, seed: int) -> dict:
    """Three usual storms, a shifted one, a usual one; then a retrain call. Returns what the app said."""
    rng = random.Random(seed + 1)
    now = pd.Timestamp.now("UTC")
    plan = [("usual", 40, 30), ("usual", 40, 20), ("usual", 40, 10), ("shifted", 40, 3), ("usual", 40, 1)]
    steps = []
    for n, (kind, count, hours_ago) in enumerate(plan):
        rows, truth = make_storm(kind, count, now - pd.Timedelta(hours=hours_ago), 2.0, f"storm{n}", rng)
        steps.append({"kind": kind, **run_storm_job(rows, truth, accuracy, rng)})
    _, report = call(APP, "POST", "/models/retrain", {})
    _, models = call(APP, "GET", "/models")
    return {"steps": steps,
            "retrain": {"ran": report.get("ran"), "promoted": report.get("promoted"), "reason": report.get("reason")},
            "model_versions": len(models.get("versions", [])), "active_model": models.get("active")}


def compose(*args: str, env: dict | None = None) -> None:
    import os

    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], check=True, env={**os.environ, **(env or {})})


def prom_query(expr: str) -> list:
    from urllib.parse import quote

    status, body = call(PROM, "GET", "/api/v1/query?query=" + quote(expr), auth=False, timeout=15)
    return body.get("data", {}).get("result", []) if status == 200 else []


def soak(minutes: float, warmup: float, fault_every: float, job_every: float, retrain_every: float,
         accuracy: float, seed: int, keep: bool, with_drift_phase: bool = False) -> dict:
    rng = random.Random(seed)
    compose("up", "-d", "--build", "--wait", env={"WARMUP": str(int(warmup)), "FAULT_EVERY": str(int(fault_every))})
    started = time.time()
    try:
        marked: set = set()
        seen: dict = {}
        cycles: list[dict] = []
        retrains: list[dict] = []
        last_retrain = started
        while time.time() < started + minutes * 60:
            time.sleep(job_every)
            _, truth = call(FLEET, "GET", "/truth", auth=False, timeout=15)
            faults = truth.get("faults", [])
            code, job = call(APP, "POST", "/jobs/run", {"mode": "ingest", "budget_k": 20})
            empty = code == 400 and "buffer is empty" in str(job.get("detail"))  # no new alert posted yet: a skip
            record = {"t": round(time.time() - started), "job_status": "skipped" if empty else code,
                      "faults_so_far": len(faults)}
            if code == 200:
                _, queue = call(APP, "GET", f"/queue?job_id={job['job_id']}&limit=2000")
                for j in queue.get("jumps", []):
                    key = (j["service"], j["start"])
                    fault = fault_for_jump(j, faults)
                    seen[key] = {"fault": fault, "confirm": _t(j["confirm"]) if j.get("confirm") else None}
                    if ("jump", *key) not in marked:
                        marked.add(("jump", *key))
                        call(APP, "POST", "/feedback", {"kind": "jump", "id": j["id"],
                                                        "verdict": engineer_verdict(fault is not None, accuracy, rng)})
                for item in queue.get("items", []):
                    if item["action"] not in ("escalate", "review"):
                        continue
                    key = ("alert", item["source"], item["timestamp"])
                    if key not in marked:
                        marked.add(key)
                        real = alert_is_fault(item["source"], item["timestamp"], faults)
                        call(APP, "POST", "/feedback", {"kind": "alert", "id": item["id"],
                                                        "verdict": engineer_verdict(real, accuracy, rng)})
                meta = job.get("meta", {})
                record.update({"model_used": meta.get("model_used"), "alerts": meta.get("alert_count"),
                               "drift": (meta.get("drift") or {}).get("status"),
                               "challenger": (meta.get("challenger") or {}).get("recommendation"),
                               "shown_jumps": len([j for j in queue.get("jumps", [])])})
            _, mon = call(APP, "GET", "/monitoring")
            record["monitoring"] = mon.get("status")
            record["precision_from_marks"] = (mon.get("model") or {}).get("precision_from_marks")
            if time.time() - last_retrain >= retrain_every:
                last_retrain = time.time()
                _, report = call(APP, "POST", "/models/retrain", {})
                retrains.append({"t": round(time.time() - started), "ran": report.get("ran"),
                                 "promoted": report.get("promoted"), "reason": report.get("reason")})
            cycles.append(record)
            print(f"[{record['t'] // 60:3d} min] job {code} faults {record['faults_so_far']} "
                  f"shown {record.get('shown_jumps')} drift {record.get('drift')} mon {record['monitoring']}", flush=True)
        _, truth = call(FLEET, "GET", "/truth", auth=False, timeout=15)
        faults = truth.get("faults", [])
        result = summarize(faults, seen, truth.get("now", time.time()))
        _, mon = call(APP, "GET", "/monitoring")
        _, health = call(APP, "GET", "/health", auth=False)
        _, models = call(APP, "GET", "/models")
        marks = (mon.get("model") or {})
        scraped = bool(prom_query("oncall_health_ok"))
        fired = {r["metric"].get("alertname"): int(float(r["value"][1])) for r in
                 prom_query('sum by (alertname) (ALERTS{alertstate="firing"})')}
        monitoring_ok = [c["monitoring"] for c in cycles if c["monitoring"]]
        result.update({
            "settings": {"minutes": minutes, "warmup_s": warmup, "fault_every_s": fault_every,
                         "job_every_s": job_every, "retrain_every_s": retrain_every, "accuracy": accuracy,
                         "seed": seed, "version": health.get("version")},
            "jobs_run": sum(1 for c in cycles if c["job_status"] == 200),
            "jobs_skipped": sum(1 for c in cycles if c["job_status"] == "skipped"),
            "jobs_failed": sum(1 for c in cycles if c["job_status"] not in (200, "skipped")),
            "marks": marks.get("marks"), "precision_from_marks": marks.get("precision_from_marks"),
            "shadow_mode": health.get("shadow_mode"),
            "retrains_run": sum(1 for r in retrains if r["ran"]), "retrain_calls": len(retrains),
            "model_versions": len(models.get("versions", [])), "active_model": models.get("active"),
            "drift_warn_cycles": sum(1 for c in cycles if c.get("drift") == "warn"),
            "drift_ok_cycles": sum(1 for c in cycles if c.get("drift") == "ok"),
            "monitoring_ok_share": round(monitoring_ok.count("ok") / len(monitoring_ok), 4) if monitoring_ok else None,
            "prometheus_scraped_app_metrics": scraped, "prometheus_alerts_firing_at_end": fired,
            "retrains": retrains, "cycles": cycles,
        })
        if with_drift_phase:
            result["drift_phase"] = drift_phase(accuracy, seed)
        return result
    finally:
        if keep:
            print("Stack left running (docker compose -f integration/live/docker-compose.yml down -v to stop).")
        else:
            compose("down", "-v")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=120)
    parser.add_argument("--warmup", type=float, default=1200, help="seconds before the first fault")
    parser.add_argument("--fault-every", type=float, default=300)
    parser.add_argument("--job-every", type=float, default=180, help="seconds between ingest jobs")
    parser.add_argument("--retrain-every", type=float, default=600)
    parser.add_argument("--accuracy", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--drift-phase", action="store_true",
                        help="after the soak, post alert storms to exercise the drift check and a retrain")
    parser.add_argument("--keep", action="store_true", help="leave the stack running afterwards")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    result = soak(args.minutes, args.warmup, args.fault_every, args.job_every, args.retrain_every,
                  args.accuracy, args.seed, args.keep, args.drift_phase)
    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    keys = ("faults_injected", "faults_judged", "faults_caught", "detection_rate", "median_delay_minutes",
            "shown_jumps", "false_jumps", "true_precision_jumps", "jobs_run", "jobs_skipped", "jobs_failed", "marks",
            "precision_from_marks", "retrains_run", "model_versions", "drift_warn_cycles", "drift_ok_cycles",
            "monitoring_ok_share", "prometheus_scraped_app_metrics", "shadow_mode")
    print(json.dumps({k: result[k] for k in keys}, indent=2))
    if "drift_phase" in result:
        print(json.dumps(result["drift_phase"], indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
