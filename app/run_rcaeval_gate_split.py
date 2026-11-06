"""RCAEval RE2 / RE3 with the promotion gate decided on cases that are never scored.

run_rcaeval_logs.py trains on the first half of each application's cases, scores
the later half, and also runs the product's promotion gate on that same later
half, so "promoted on Train Ticket" was decided on its own test cases. Here the
cases are split in three, in start order:

  train       first 50%   fit the alert classifier, its threshold, the confirm gates
  validation  next 25%    the product's promotion gate decides on these only
  test        last 25%    scored once; never seen by the fit or the gate

Everything else (log rules, jump rule, chance handling, labels) is imported from
run_rcaeval_logs.py. "shipped" is what the product would run on the test cases:
the classifier if the gate promoted it, otherwise log severity (the product's
rules-only fallback). Each suite also gets a pooled row over its three
applications, with a 95% bootstrap interval over the pooled test cases.

    python run_rcaeval_gate_split.py            # RE2 and RE3
    python run_rcaeval_gate_split.py RE2

Writes results/rcaeval_gate_split.csv and results/rcaeval_gate_split.json.
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pandas as pd

from run_rcaeval_logs import (
    BUDGETS,
    HEADLINE_K,
    MANIFEST,
    ORIGIN,
    ROOT,
    SUITES,
    SYSTEMS,
    VARIANTS,
    _load_one,
    _plain,
    base_config,
    early_in_recording,
    load_cases,
    per_case,
    product_gate,
    wilson,
)
from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.eval_stats import bootstrap_mean_ci
from src.features import build_feature_frame
from src.memory_spikes import detect_jumps
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold
from src.queue import (
    alert_items,
    build_queue,
    n_days_between,
    score_queue,
    spike_items,
    tune_confirm_thresholds,
)
from src.tracking import record_run

TRAIN_FRACTION = 0.5
VALIDATION_FRACTION = 0.25
QUEUES = (*VARIANTS, "shipped")


def split_sizes(n: int) -> tuple[int, int, int]:
    n_train = int(n * TRAIN_FRACTION)
    n_val = int(n * VALIDATION_FRACTION)
    return n_train, n_val, n - n_train - n_val


def run_app(suite: str, system: str, workers: int) -> tuple[list[dict], dict, dict[str, list[float]]]:
    config = base_config()
    cases = load_cases(suite, system)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        loaded = list(pool.map(_load_one, cases))
    n_train, n_val, n_test = split_sizes(len(cases))
    val_start = ORIGIN + pd.Timedelta(days=n_train)
    test_start = ORIGIN + pd.Timedelta(days=n_train + n_val)
    faults = [c.as_fault() for c in cases]
    alerts = sorted((a for one in loaded for a in one[0]), key=lambda a: a.timestamp)
    all_metrics = pd.concat([one[1] for one in loaded], ignore_index=True)
    del loaded
    jumps = [j for j in detect_jumps(all_metrics, config) if not early_in_recording(j)]
    train_alerts = [a for a in alerts if a.timestamp < val_start]
    test_alerts = [a for a in alerts if a.timestamp >= test_start]
    train_faults, test_faults = faults[:n_train], faults[n_train + n_val:]
    train_jumps = [j for j in jumps if j.shown_at < val_start]
    test_jumps = [j for j in jumps if j.shown_at >= test_start]
    end = max(f.end for f in test_faults) + pd.Timedelta(seconds=1)
    n_days = n_days_between(test_start, end, config)
    label = f"{suite} {SYSTEMS[system]}"

    # The gate sees train and validation only: nothing at or after test_start.
    before_test = all_metrics["timestamp"] < test_start
    gate = product_gate([a for a in alerts if a.timestamp < test_start], all_metrics[before_test],
                        faults[:n_train + n_val], val_start, config, HEADLINE_K)
    promoted = bool(gate["promoted"])
    info = {"suite": suite, "app": SYSTEMS[system], "train_cases": n_train, "validation_cases": n_val,
            "test_cases": n_test, "promotion_gate": gate, "promoted": promoted, "by_k": {}}

    rows: list[dict] = []
    pooled: dict[str, list[float]] = {name: [] for name in QUEUES}
    two_classes = len({a.fault_id is not None for a in train_alerts}) == 2
    for k in BUDGETS:
        cfg = replace(config, budget_k=k)
        thr, by_series = tune_confirm_thresholds(train_jumps, train_faults, cfg, train_until=val_start)
        tuned = replace(cfg, confirm_min_rise=thr, confirm_min_rise_by_series=by_series)
        severity = baseline_decisions(test_alerts, cfg)
        if two_classes and test_alerts:
            model = AlertClassifier(cfg).fit(train_alerts, train_jumps)
            alert_thr = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_jumps), cfg)
            proba = model.predict_proba(test_alerts, test_jumps)
            in_jump = [bool(v) for v in build_feature_frame(test_alerts, cfg, test_jumps)["during_memory_spike"]]
            learned = spike_review_decisions(test_alerts, proba, cfg, alert_thr, bayes_threshold(cfg), in_jump)
        else:
            learned = severity
        chosen = {"classifier": learned, "severity": severity, "no_alerts": None,
                  "shipped": learned if promoted else severity}
        outcome = {}
        for name in QUEUES:
            items = spike_items(test_jumps, tuned)
            decisions = chosen[name]
            if decisions is not None:
                items += alert_items(test_alerts, decisions)
            shown = build_queue(items, tuned)
            row = score_queue(name, shown, test_faults, n_days, tuned)
            outcome[name] = per_case(shown, test_faults, tuned)
            low, high = wilson(row["faults_caught"], row["faults"])
            row.update({"suite": suite, "app": SYSTEMS[system], "ci_low": low, "ci_high": high,
                        "promoted": promoted})
            rows.append(row)
            if k == HEADLINE_K:
                pooled[name] = [float(outcome[name][f.fault_id][0]) for f in test_faults]
            print(f"{label:22s} K={k:2d} {name:10s} caught {row['faults_caught']:2d}/{row['faults']} "
                  f"({row['detection_rate']:.1%})  false/day {row['false_items_per_day']:5.2f}")
    print(f"{label:22s} gate on validation: {'promoted' if promoted else 'refused'} ({gate['gate']})")
    return rows, info, pooled


def pooled_summary(suite: str, pooled: dict[str, list[float]]) -> dict:
    out: dict = {"suite": suite, "k": HEADLINE_K, "test_cases": len(pooled["no_alerts"])}
    for name in QUEUES:
        ci = bootstrap_mean_ci(pooled[name])
        out[name] = [round(ci.mean, 4), round(ci.low, 4), round(ci.high, 4)]
    for name, other in (("shipped", "no_alerts"), ("shipped", "severity"), ("classifier", "no_alerts")):
        ci = bootstrap_mean_ci([a - b for a, b in zip(pooled[name], pooled[other])])
        out[f"{name}_minus_{other}"] = [round(ci.mean, 4), round(ci.low, 4), round(ci.high, 4)]
    return out


def main(argv: list[str]) -> None:
    suites = tuple(s.upper() for s in argv) or SUITES
    if not (ROOT / "cases.parquet").exists():
        sys.exit("RCAEval RE2/RE3 not found: run python ../tools/fetch_rcaeval_re23.py first")
    workers = min(4, max(1, (os.cpu_count() or 2) - 1))
    rows: list[dict] = []
    infos, summaries = [], []
    for suite in suites:
        pooled = {name: [] for name in QUEUES}
        for system in SYSTEMS:
            app_rows, info, app_pooled = run_app(suite, system, workers)
            rows += app_rows
            infos.append(info)
            for name in QUEUES:
                pooled[name] += app_pooled[name]
        summary = pooled_summary(suite, pooled)
        summaries.append(summary)
        print(f"{suite} pooled over {summary['test_cases']} test cases at K={HEADLINE_K}: "
              f"shipped {summary['shipped'][0]:.1%}, no_alerts {summary['no_alerts'][0]:.1%}, "
              f"shipped minus no_alerts {summary['shipped_minus_no_alerts']}")
    tag = "" if suites == SUITES else "_" + "_".join(s.lower() for s in suites)
    out = Path("results") / f"rcaeval_gate_split{tag}.csv"
    out.parent.mkdir(exist_ok=True)
    front = ["suite", "app", "budget_k", "queue", "faults", "faults_caught", "detection_rate", "ci_low", "ci_high"]
    frame = pd.DataFrame(rows)
    frame[front + [c for c in frame.columns if c not in front]].to_csv(out, index=False)
    notes = out.with_suffix(".json")
    settings = {"train_fraction": TRAIN_FRACTION, "validation_fraction": VALIDATION_FRACTION,
                "budgets": BUDGETS, "headline_k": HEADLINE_K}
    notes.write_text(json.dumps({"settings": settings, "apps": infos, "pooled": summaries}, indent=2,
                                default=_plain) + "\n", encoding="utf-8")
    print(f"Results written to {out} and {notes}")
    head = frame[frame["budget_k"] == HEADLINE_K]
    record_run(
        "rcaeval_gate_split" + tag,
        config=base_config(),
        data=[ROOT / "cases.parquet", MANIFEST],
        outputs=[out, notes],
        metrics={f"K{HEADLINE_K}.{r.suite}.{r.app}.{r.queue}": r.detection_rate for r in head.itertuples()},
    )


if __name__ == "__main__":
    main(sys.argv[1:])
