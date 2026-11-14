"""Second dataset for the log-alert channel: RCAEval RE2 and RE3 (logs + metrics).

Needs `python ../tools/fetch_rcaeval_re23.py` first (about 570 MB, checksummed).
GAIA is one system; this asks whether the alert classifier's result (it does not
add catches to the queue, docs/report_appendices_full.md section B4) holds on three other
applications. Each (suite, application) is scored on its own:

  RE2  network/resource faults (cpu, mem, disk, delay, loss, socket), 90 cases each
  RE3  code-level faults (F1-F5), 30 cases each

How a recording becomes queue input (all choices are fixed here, not tuned):

* Cases are ordered by start time. The first half trains (alert classifier,
  its threshold, the jump confirm gate); the later half is scored once.
* Each case is laid on its own on-call day (ORIGIN + i days + 1 h), so the
  daily budget K is per case. Recordings are 24-30 minutes, half before the
  injection; the fault window is [injection, end of recording] on the root-cause
  service.
* Metric channel: the product's built-in jump rule in its unit-free robust mode
  (memory_jump_mode="robust") on each service's cpu, mem, diskio, socket,
  latency-90 and error series (workload is the load generator's request rate and
  latency-50 duplicates latency-90). Jumps are found on the laid-out timeline
  exactly as the product finds them (so the gate below sees the same items);
  the rolling median is time-based, so one recording's last sample is never the
  next one's baseline. But the 15-minute baseline is nearly empty in the first
  minutes of each recording, which live data (one continuous stream) never
  has, and nearly half of all jumps start there. The ablation drops jumps that
  start in the first WARMUP_MINUTES of a recording (all before the injection;
  counted in the JSON as `early_jumps`). The product gate below cannot be told
  to, so its queue check keeps them (they cost the three queues the same slots).
  Confirm gates are tuned on the training cases per series, as the product does.
  Leaks (byte rule) and monitoring gaps are off: the silences between recordings
  are not real gaps.
* Log channel: a log line is an alert only at warning or error level. The level
  is the first ERROR/FATAL/SEVERE/CRITICAL/PANIC or WARN/WARNING token; a line
  with no level token is an error if it says exception/error/fail/panic/fatal/
  traceback/refused/denied/timeout/unavailable, otherwise it is dropped (info).
  Identical lines (digits and hex ids masked) from one service within a
  COLLAPSE_SECONDS bucket are one alert.
* Chance check: the same queue scored against a fake fault on the root-cause
  service in the quiet part of each test recording (after the warm-up, before
  the injection: 7 minutes against up to 12 after it, 10 against 15 for RE3 Train
  Ticket). `placebo_caught` in the JSON; a channel that only fires at random
  would "catch" these too.
* Label: an alert carries the case's fault id when it comes from the root-cause
  service after the injection (the same service rule as GAIA). Alerts from
  callers of a broken service count as noise under this rule.

Variants, as in run_classifier_ablation.py: `classifier`, `severity`,
`no_alerts`. Detection rates get 95% Wilson intervals; the paired difference
to `no_alerts` gets a 95% bootstrap interval over test cases. The product's
promotion gate (product/pipeline.score_alerts, no saved model) is run on each
application with the same split and the same metric settings.

    python run_rcaeval_logs.py                 # RE2 and RE3
    python run_rcaeval_logs.py RE3             # one suite

Writes results/rcaeval_logs.csv (one row per suite, app, K, variant) and
results/rcaeval_logs.json (CIs, gate decisions, data counts).
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config, day_of
from src.entities import Action, Alert, Fault
from src.eval_stats import bootstrap_mean_ci
from src.features import build_feature_frame
from src.memory_spikes import Spike, detect_jumps
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold
from src.queue import (
    QueueItem,
    alert_items,
    build_queue,
    n_days_between,
    oracle_ceiling,
    score_queue,
    spike_items,
    tune_confirm_thresholds,
)
from src.tracking import record_run

ROOT = Path("data/rcaeval_re23")
MANIFEST = Path("../tools/rcaeval_re23_sha256.txt")
ORIGIN = pd.Timestamp("2024-01-01", tz="UTC")
DAY_OFFSET = pd.Timedelta(hours=1)
SUITES = ("RE2", "RE3")
SYSTEMS = {"ob": "Online Boutique", "ss": "Sock Shop", "tt": "Train Ticket"}
BUDGETS = (5, 10, 20)
HEADLINE_K = 10
VARIANTS = ("classifier", "severity", "no_alerts")
METRIC_KINDS = ("cpu", "mem", "diskio", "socket", "latency-90", "error")
WARMUP_MINUTES = 5
COLLAPSE_SECONDS = 60

LEVEL = re.compile(r"\b(ERROR|FATAL|SEVERE|CRITICAL|PANIC|WARN|WARNING|INFO|DEBUG|TRACE)\b")
ERRORISH = re.compile(r"exception|error|fail|panic|fatal|traceback|refused|denied|timed? ?out|unavailable", re.I)
MASK = re.compile(r"0x[0-9a-f]+|[0-9a-f]{8,}|\d+", re.I)


def base_config() -> Config:
    """Product defaults, with the unit-free jump rule and no leak or gap items."""
    return Config(memory_jump_mode="robust", leak_window_minutes=None, gap_minutes=10**6)


def log_severity(message: str) -> str | None:
    """'error', 'warning', or None (info / debug: not an alert)."""
    found = LEVEL.search(message[:200])
    if found:
        token = found.group(1)
        if token in ("ERROR", "FATAL", "SEVERE", "CRITICAL", "PANIC"):
            return "error"
        return "warning" if token.startswith("WARN") else None
    return "error" if ERRORISH.search(message) else None


def mask(message: str) -> str:
    return MASK.sub("#", message.strip()[:300])


@dataclass
class Case:
    case: str
    day: int
    start: int  # unix seconds
    inject: int
    end: int
    root: str
    fault: str

    def at(self, seconds: float) -> pd.Timestamp:
        """Case time (unix seconds) on its own on-call day."""
        return ORIGIN + pd.Timedelta(days=self.day) + DAY_OFFSET + pd.Timedelta(seconds=float(seconds) - self.start)

    def as_fault(self) -> Fault:
        return Fault(self.case, self.at(self.inject), self.at(self.end), self.root, self.fault)

    def placebo(self) -> Fault:
        """Same service, before the injection (after the warm-up): nothing is broken."""
        return Fault(f"{self.case}-placebo", self.at(self.start + 60 * WARMUP_MINUTES), self.at(self.inject),
                     self.root, "none")


def case_alerts(case: Case) -> list[Alert]:
    path = ROOT / case.case / "logs.parquet"
    if not path.exists():  # one RE2 case ships no logs
        return []
    logs = pd.read_parquet(path, columns=["timestamp", "container_name", "message"]).dropna()
    logs = logs[(logs["timestamp"] >= case.start) & (logs["timestamp"] <= case.end)]
    severity = logs["message"].map(log_severity)
    logs = logs.assign(severity=severity)[severity.notna()]
    if logs.empty:
        return []
    logs = logs.assign(
        norm=logs["message"].map(mask),
        bucket=((logs["timestamp"] - case.start) // COLLAPSE_SECONDS).astype(int),
    ).sort_values("timestamp", kind="stable")
    first = logs.drop_duplicates(["container_name", "norm", "bucket"], keep="first")
    out = []
    for n, row in enumerate(first.itertuples(index=False)):
        source = str(row.container_name)
        label = case.case if source == case.root and row.timestamp >= case.inject else None
        out.append(Alert(f"{case.case}-{n}", case.at(row.timestamp), source, "log", str(row.severity),
                         str(row.message)[:300], label))
    return out


def case_metrics(case: Case) -> pd.DataFrame:
    raw = pd.read_parquet(ROOT / case.case / "metrics.parquet")
    cols = [c for c in raw.columns if c != "time" and c.rsplit("_", 1)[-1] in METRIC_KINDS]
    long = raw[["time", *cols]].melt("time", var_name="series_id", value_name="value").dropna()
    long = long[(long["time"] >= case.start) & (long["time"] <= case.end)]
    seconds = (long["time"].to_numpy(dtype=float) - case.start) * 1e9
    return pd.DataFrame({
        "timestamp": case.at(case.start) + pd.to_timedelta(seconds.astype("int64"), unit="ns"),
        "value": long["value"].astype(float).to_numpy(),
        "service": long["series_id"].map({c: c.rsplit("_", 1)[0] for c in cols}).to_numpy(),
        "series_id": long["series_id"].to_numpy(),
    })


def early_in_recording(jump: Spike) -> bool:
    """Starts in the first WARMUP_MINUTES of its recording (short rolling baseline)."""
    since = (jump.start - (ORIGIN + DAY_OFFSET)) % pd.Timedelta(days=1)
    return bool(since < pd.Timedelta(minutes=WARMUP_MINUTES))


def _load_one(case: Case) -> tuple[list[Alert], pd.DataFrame]:
    return case_alerts(case), case_metrics(case)


def load_cases(suite: str, system: str) -> list[Case]:
    index = pd.read_parquet(ROOT / "cases.parquet")
    rows = index[(index["suite"] == suite) & (index["system"] == system)].sort_values(["time_start", "case"])
    return [
        Case(r.case, i, int(r.time_start), int(r.inject_time), int(r.time_end), str(r.root_cause_service), str(r.fault))
        for i, r in enumerate(rows.itertuples(index=False))
    ]


def wilson(hits: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def per_case(shown: list[QueueItem], faults: list[Fault], config: Config) -> dict[str, tuple[int, int]]:
    """fault id -> (caught 0/1, false items that day). One fault per day here."""
    by_day: dict[pd.Timestamp, list[QueueItem]] = {}
    for item in shown:
        by_day.setdefault(day_of(item.shown_at, config), []).append(item)
    out = {}
    for fault in faults:
        row = score_queue("x", by_day.get(day_of(fault.start, config), []), [fault], 1, config)
        out[fault.fault_id] = (row["faults_caught"], int(round(row["false_items_per_day"])))
    return out


def product_gate(alerts: list[Alert], metrics: pd.DataFrame, faults: list[Fault], split_at: pd.Timestamp,
                 config: Config, k: int) -> dict:
    """What the product decides on this application: fit, queue check, promotion gate."""
    from product.pipeline import promotion_margin, score_alerts

    if not alerts:
        return {"model_used": "rules_only", "promoted": False, "gate": "no log alerts", "queue_check": None}
    origin = min(a.timestamp for a in alerts)
    # score_alerts splits at the first alert + split_day days; pick split_day so that
    # this is the same case boundary as the ablation.
    split_day = max(1, int((split_at - day_of(origin, config)).days))
    cfg = replace(config, budget_k=k, split_day=split_day)
    result = score_alerts(alerts, metrics=metrics, budget_k=k, saved=None, faults=faults, config=cfg,
                          use_saved_detector=False)
    training = result.training or {}
    test = training.get("test") or {}
    check = training.get("queue_check")
    saving = (min(check["cost_no_log_alerts"], check.get("cost_fallback", check["cost_no_log_alerts"]))
              - check["cost_with_model"]) if check else None
    return {
        "budget_k": k,
        "split_day": split_day,
        "margin": promotion_margin(cfg),
        "queue_saving": saving,
        "model_used": result.model_used,
        "promoted": bool(result.promoted),
        "gate": training.get("gate") or training.get("note"),
        "held_out_alerts": {key: test.get(key) for key in ("alerts", "fault_alerts", "tp", "fp", "fn")},
        "queue_check": training.get("queue_check"),
    }


def run_app(suite: str, system: str, workers: int) -> tuple[list[dict], dict]:
    config = base_config()
    cases = load_cases(suite, system)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        loaded = list(pool.map(_load_one, cases))
    n_train = len(cases) // 2
    split_at = ORIGIN + pd.Timedelta(days=n_train)
    faults = [c.as_fault() for c in cases]
    alerts = sorted((a for one in loaded for a in one[0]), key=lambda a: a.timestamp)
    all_metrics = pd.concat([one[1] for one in loaded], ignore_index=True)
    del loaded  # Train Ticket RE2 is about 39 million metric rows: keep one copy
    jumps = detect_jumps(all_metrics, config)
    product_jumps = len(jumps)
    jumps = [j for j in jumps if not early_in_recording(j)]
    train_alerts = [a for a in alerts if a.timestamp < split_at]
    test_alerts = [a for a in alerts if a.timestamp >= split_at]
    train_faults, test_faults = faults[:n_train], faults[n_train:]
    train_jumps = [j for j in jumps if j.shown_at < split_at]
    test_jumps = [j for j in jumps if j.shown_at >= split_at]
    end = max(f.end for f in test_faults) + pd.Timedelta(seconds=1)
    n_days = n_days_between(split_at, end, config)
    label = f"{suite} {SYSTEMS[system]}"
    info = {
        "suite": suite, "app": SYSTEMS[system], "cases": len(cases), "train_cases": n_train,
        "test_cases": len(test_faults),
        "test_faults_by_type": pd.Series([c.fault for c in cases[n_train:]]).value_counts().to_dict(),
        "train_alerts": len(train_alerts), "test_alerts": len(test_alerts),
        "train_fault_alerts": sum(a.fault_id is not None for a in train_alerts),
        "test_fault_alerts": sum(a.fault_id is not None for a in test_alerts),
        "test_cases_with_fault_alert": len({a.fault_id for a in test_alerts if a.fault_id}),
        "jumps": product_jumps,
        "early_jumps": product_jumps - len(jumps),
        "test_jumps": len(test_jumps),
        "by_k": {},
    }
    rows = []
    two_classes = len({a.fault_id is not None for a in train_alerts}) == 2
    for k in BUDGETS:
        cfg = replace(config, budget_k=k)
        thr, by_series = tune_confirm_thresholds(train_jumps, train_faults, cfg, train_until=split_at)
        tuned = replace(cfg, confirm_min_rise=thr, confirm_min_rise_by_series=by_series)
        severity = baseline_decisions(test_alerts, cfg)
        if two_classes and test_alerts:
            model = AlertClassifier(cfg).fit(train_alerts, train_jumps)
            alert_thr = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_jumps), cfg)
            proba = model.predict_proba(test_alerts, test_jumps)
            in_jump = [bool(v) for v in build_feature_frame(test_alerts, cfg, test_jumps)["during_memory_spike"]]
            learned = spike_review_decisions(test_alerts, proba, cfg, alert_thr, bayes_threshold(cfg), in_jump)
        else:
            learned = severity  # no fault alert in training: nothing to learn
        chosen: dict[str, list | None] = {"classifier": learned, "severity": severity, "no_alerts": None}
        outcome = {}
        for name in VARIANTS:
            items = spike_items(test_jumps, tuned)  # fresh items per queue: build_queue marks them
            decisions = chosen[name]
            if decisions is not None:
                items += alert_items(test_alerts, decisions)
            shown = build_queue(items, tuned)
            row = score_queue(name, shown, test_faults, n_days, tuned)
            outcome[name] = per_case(shown, test_faults, tuned)
            if name == "no_alerts":
                # A metric item on the root-cause service in the quiet part of the
                # recording: how often the metric channel "catches" by chance.
                fake = score_queue("placebo", shown, [c.placebo() for c in cases[n_train:]], n_days, tuned)
                info["by_k"].setdefault(str(k), {})["placebo_caught"] = fake["faults_caught"]
            assert sum(c for c, _ in outcome[name].values()) == row["faults_caught"]
            low, high = wilson(row["faults_caught"], row["faults"])
            row.update({
                "suite": suite, "app": SYSTEMS[system], "ci_low": low, "ci_high": high,
                "oracle": round(oracle_ceiling(test_faults, tuned), 4),
                "escalated_alerts": sum(d.action == Action.ESCALATE for d in chosen[name] or []),
                "alerts_shown": sum(1 for i in shown if i.kind == "alert" and not i.attached),
            })
            rows.append(row)
            print(f"{label:22s} K={k:2d} {name:10s} caught {row['faults_caught']:2d}/{row['faults']} "
                  f"({row['detection_rate']:.1%}, CI {low:.0%}-{high:.0%})  false/day {row['false_items_per_day']:5.2f}")
        diffs = {}
        for name in ("classifier", "severity"):
            delta = [outcome[name][f.fault_id][0] - outcome["no_alerts"][f.fault_id][0] for f in test_faults]
            ci = bootstrap_mean_ci([float(d) for d in delta])
            false_delta = [outcome[name][f.fault_id][1] - outcome["no_alerts"][f.fault_id][1] for f in test_faults]
            fci = bootstrap_mean_ci([float(d) for d in false_delta])
            diffs[f"{name}_minus_no_alerts"] = {
                "detection": [round(ci.mean, 4), round(ci.low, 4), round(ci.high, 4)],
                "false_items_per_day": [round(fci.mean, 3), round(fci.low, 3), round(fci.high, 3)],
                "cases_gained": sum(d > 0 for d in delta), "cases_lost": sum(d < 0 for d in delta),
            }
        info["by_k"][str(k)].update({"confirm_min_rise": thr, "series_gates": len(by_series), **diffs})
    info["promotion_gate"] = product_gate(alerts, all_metrics, faults, split_at, config, HEADLINE_K)
    print(f"{label:22s} gate: {info['promotion_gate']['model_used']} / {info['promotion_gate']['gate']}")
    return rows, info


def main(argv: list[str]) -> None:
    suites = tuple(s.upper() for s in argv) or SUITES
    if not (ROOT / "cases.parquet").exists():
        sys.exit("RCAEval RE2/RE3 not found: run python ../tools/fetch_rcaeval_re23.py first")
    workers = min(4, max(1, (os.cpu_count() or 2) - 1))  # each holds a case in memory
    rows: list[dict] = []
    infos = []
    for suite in suites:
        for system in SYSTEMS:
            app_rows, info = run_app(suite, system, workers)
            rows += app_rows
            infos.append(info)
    tag = "" if suites == SUITES else "_" + "_".join(s.lower() for s in suites)
    out = Path("results") / f"rcaeval_logs{tag}.csv"
    out.parent.mkdir(exist_ok=True)
    front = ["suite", "app", "budget_k", "queue", "faults", "faults_caught", "detection_rate", "ci_low", "ci_high"]
    frame = pd.DataFrame(rows)
    frame[front + [c for c in frame.columns if c not in front]].to_csv(out, index=False)
    notes = Path("results") / f"rcaeval_logs{tag}.json"
    settings = {"metric_kinds": METRIC_KINDS, "warmup_minutes": WARMUP_MINUTES, "collapse_seconds": COLLAPSE_SECONDS,
                "budgets": BUDGETS, "headline_k": HEADLINE_K, "train_fraction": 0.5}
    notes.write_text(json.dumps({"settings": settings, "apps": infos}, indent=2, default=_plain) + "\n",
                     encoding="utf-8")
    print(f"Results written to {out} and {notes}")
    head = frame[frame["budget_k"] == HEADLINE_K]
    record_run(
        "rcaeval_logs" + tag,
        config=base_config(),
        data=[ROOT / "cases.parquet", MANIFEST],
        outputs=[out, notes],
        metrics={f"K{HEADLINE_K}.{r.suite}.{r.app}.{r.queue}": r.detection_rate for r in head.itertuples()},
    )


def _plain(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return str(value)


if __name__ == "__main__":
    main(sys.argv[1:])
