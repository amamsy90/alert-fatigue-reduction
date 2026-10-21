"""Queue scoring on a planted series.

The expected catch is the requirement: one closed test-window fault, a memory
series that rises to three times its recent level for longer than two samples
a minute apart (inside the two-minute gap the detector uses), on that fault's
service, with K=20. jumps_online shows jumps without the confirm gate.
A series that is not longer than seven days is not scored, and no detection
rate is written.
"""
from __future__ import annotations

import json
import os

import pandas as pd
from homelab.evaluate import _population, lab_config, leave_one_company_out, run, score_population, write_result

ORIGIN = pd.Timestamp("2026-01-01T00:00:00Z")
END = pd.Timestamp("2026-01-09T18:00:00Z")


def _series(company: str, jumps: list[tuple[pd.Timestamp, pd.Timestamp, float]]) -> pd.DataFrame:
    index = pd.date_range(ORIGIN, END, freq="1min", inclusive="left")
    values = pd.Series(1.0e8, index=index)
    for start, stop, level in jumps:
        values.loc[(index >= start) & (index < stop)] = level
    hole = (index >= pd.Timestamp("2026-01-02T08:00:00Z")) & (index < pd.Timestamp("2026-01-02T08:40:00Z"))
    frame = pd.DataFrame(
        {
            "timestamp": index[~hole],
            "value": values.loc[~hole].to_numpy(),
            "service": f"{company}/frontend",
            "company": company,
            "metric": "memory_working_set_bytes",
        }
    )
    return frame


def _alert(company: str, when: str, severity: str, alert_id: str) -> dict:
    return {
        "alert_id": alert_id,
        "timestamp": pd.Timestamp(when),
        "source": f"{company}/frontend",
        "alert_type": "HomelabCpuHigh",
        "severity": severity,
        "message": alert_id,
        "company": company,
    }


def _fault(company: str, fault_id: str, start: str, end: str) -> dict:
    return {
        "fault_id": fault_id,
        "start": pd.Timestamp(start),
        "end": pd.Timestamp(end),
        "component": f"{company}/frontend",
        "description": fault_id,
        "company": company,
        "fault_type": "cpu-stress",
        "service": "frontend",
        "status": "closed",
    }


def lab_frames():
    acme = _series(
        "acme",
        [
            (pd.Timestamp("2026-01-01T06:00:00Z"), pd.Timestamp("2026-01-01T06:20:00Z"), 1.8e8),
            (pd.Timestamp("2026-01-03T12:00:00Z"), pd.Timestamp("2026-01-03T12:40:00Z"), 8.0e8),
            (pd.Timestamp("2026-01-09T10:00:00Z"), pd.Timestamp("2026-01-09T10:30:00Z"), 3.0e8),
        ],
    )
    borealis = _series(
        "borealis",
        [
            (pd.Timestamp("2026-01-01T06:00:00Z"), pd.Timestamp("2026-01-01T06:20:00Z"), 1.35e8),
            (pd.Timestamp("2026-01-03T12:00:00Z"), pd.Timestamp("2026-01-03T12:40:00Z"), 1.5e8),
        ],
    )
    metrics = pd.concat([acme, borealis], ignore_index=True)
    alerts = pd.DataFrame(
        [
            _alert("acme", "2026-01-03T12:10:00Z", "critical", "acme-fault"),
            _alert("acme", "2026-01-04T03:00:00Z", "critical", "acme-noise"),
            _alert("acme", "2026-01-09T10:10:00Z", "critical", "acme-test"),
            _alert("borealis", "2026-01-03T12:10:00Z", "critical", "borealis-fault"),
            _alert("borealis", "2026-01-04T03:00:00Z", "critical", "borealis-noise"),
            _alert("borealis", "2026-01-09T15:10:00Z", "critical", "borealis-test"),
        ]
    )
    faults = pd.DataFrame(
        [
            _fault("acme", "acme-week1", "2026-01-03T12:00:00Z", "2026-01-03T12:45:00Z"),
            _fault("acme", "acme-week2", "2026-01-09T10:00:00Z", "2026-01-09T10:40:00Z"),
            _fault("borealis", "borealis-week1", "2026-01-03T12:00:00Z", "2026-01-03T12:45:00Z"),
            _fault("borealis", "borealis-week2", "2026-01-09T15:00:00Z", "2026-01-09T15:20:00Z"),
        ]
    )
    return alerts, metrics, faults


def test_relative_jump_in_the_test_window_is_caught_at_k20():
    alerts, metrics, faults = lab_frames()
    frame, alert_objs, fault_objs = _population(alerts, metrics, faults, "acme")
    report = score_population("acme", frame, alert_objs, fault_objs, lab_config(), [20])
    assert report["scored"] is True
    row = next(item for item in report["rows"] if item["queue"] == "jumps_online" and item["budget_k"] == 20)
    assert row["faults"] == 1
    assert row["faults_caught"] == 1


def test_short_series_is_not_scored_and_has_no_detection_rate(tmp_path):
    index = pd.date_range(ORIGIN, periods=2 * 24 * 60, freq="1min")
    metrics = pd.DataFrame(
        {
            "timestamp": index,
            "value": 1.0e8,
            "service": "acme/frontend",
            "company": "acme",
            "metric": "memory_working_set_bytes",
        }
    )
    alerts = pd.DataFrame([_alert("acme", "2026-01-01T01:00:00Z", "critical", "only")])
    faults = pd.DataFrame([_fault("acme", "early", "2026-01-02T01:00:00Z", "2026-01-02T01:10:00Z")])
    frame, alert_objs, fault_objs = _population(alerts, metrics, faults, "acme")
    report = score_population("acme", frame, alert_objs, fault_objs, lab_config(), [20])
    assert report["scored"] is False
    assert "detection_rate" not in json.dumps(report)
    result = {
        "product_flags": {"LEARNED_RANKER": "off"},
        "per_company": {},
        "pooled": {},
        "leave_one_company_out": {"status": "off"},
        "_reports": {"per_company": {"acme": report}, "pooled": report},
    }
    write_result(result, tmp_path)
    assert not (tmp_path / "per_company" / "acme" / "queue_eval.csv").exists()
    body = (tmp_path / "per_company" / "acme" / "not_scored.json").read_text(encoding="utf-8")
    assert "detection_rate" not in body


def test_leave_one_company_out_uses_the_keep_rule_and_the_other_companies_gate():
    from src.eval_stats import bootstrap_mean_ci
    from src.memory_spikes import detect_jumps
    from src.queue import tune_confirm_thresholds

    alerts, metrics, faults = lab_frames()
    config = lab_config()
    before = {name: os.environ.get(name) for name in ("LEARNED_RANKER", "METRIC_MODEL", "LLM_EXPLAIN")}
    report = leave_one_company_out(alerts, metrics, faults, ["acme", "borealis"], config)
    after = {name: os.environ.get(name) for name in ("LEARNED_RANKER", "METRIC_MODEL", "LLM_EXPLAIN")}
    assert before == after
    assert report["bootstrap_draws"] == 4000
    assert report["bootstrap_seed"] == 42
    scored = [company for company, entry in report["companies"].items() if entry["status"] == "scored"]
    assert scored
    for entry in report["companies"].values():
        if entry["status"] != "scored":
            assert entry["decision"] == "skipped"
            assert entry["per_fault_catch_delta"] is None
            continue
        delta = entry["per_fault_catch_delta"]["values"]
        interval = bootstrap_mean_ci(delta, n_draw=4000, seed=42)
        assert entry["per_fault_catch_delta"]["low"] == round(interval.low, 4)
        assert entry["per_fault_catch_delta"]["high"] == round(interval.high, 4)
        assert (entry["decision"] == "keep") is (interval.low > 0)

    borealis_metrics, _alerts, borealis_faults = _population(alerts, metrics, faults, "borealis")
    train_end = pd.Timestamp(borealis_metrics["timestamp"].min()) + pd.Timedelta(days=7)
    other_gate, _by = tune_confirm_thresholds(
        detect_jumps(borealis_metrics, config, detect_until=train_end),
        [fault for fault in borealis_faults if fault.start < train_end],
        config,
        train_until=train_end,
    )
    acme_metrics, _alerts, acme_faults = _population(alerts, metrics, faults, "acme")
    acme_end = pd.Timestamp(acme_metrics["timestamp"].min()) + pd.Timedelta(days=7)
    own_gate, _by = tune_confirm_thresholds(
        detect_jumps(acme_metrics, config, detect_until=acme_end),
        [fault for fault in acme_faults if fault.start < acme_end],
        config,
        train_until=acme_end,
    )
    assert report["companies"]["acme"]["confirm_min_rise"] == (None if other_gate is None else float(other_gate))
    assert report["companies"]["acme"]["confirm_min_rise"] != (None if own_gate is None else float(own_gate))


def test_default_run_leaves_the_ranker_off_and_skips_cluster(monkeypatch):
    monkeypatch.delenv("LEARNED_RANKER", raising=False)
    monkeypatch.delenv("METRIC_MODEL", raising=False)
    monkeypatch.delenv("LLM_EXPLAIN", raising=False)
    index = pd.date_range(ORIGIN, periods=3 * 60, freq="1min")
    metrics = pd.DataFrame(
        {
            "timestamp": list(index) + list(index),
            "value": 1.0e8,
            "service": ["acme/frontend"] * len(index) + ["cluster/node"] * len(index),
            "company": ["acme"] * len(index) + ["cluster"] * len(index),
            "metric": "memory_working_set_bytes",
        }
    )
    alerts = pd.DataFrame(
        [
            _alert("acme", "2026-01-01T01:00:00Z", "info", "noise"),
            {
                "alert_id": "cluster-1",
                "timestamp": pd.Timestamp("2026-01-01T01:05:00Z"),
                "source": "cluster/node",
                "alert_type": "Watchdog",
                "severity": "none",
                "message": "watchdog",
                "company": "cluster",
            },
        ]
    )
    faults = pd.DataFrame([_fault("acme", "early", "2026-01-01T02:00:00Z", "2026-01-01T02:10:00Z")])
    result = run(
        alerts,
        metrics,
        faults,
        budgets=[20],
        split_day=7,
        learned_ranker=False,
        company_ids=["acme", "cluster"],
    )
    assert "cluster" not in result["per_company"]
    assert result["per_company"]["acme"]["scored"] is False
    assert "detection_rate" not in result["per_company"]["acme"]
    loco = result["leave_one_company_out"]
    assert loco["status"] == "off"
    blob = json.dumps(loco)
    assert "ci_low" not in blob
    assert "per_fault_catch_delta" not in blob
    assert result["flags_changed"] is False
    assert result["product_flags"]["LEARNED_RANKER"] == "off"
    assert os.environ.get("LEARNED_RANKER") is None
    assert os.environ.get("METRIC_MODEL") is None
    assert os.environ.get("LLM_EXPLAIN") is None
