"""load_tables must keep faults.csv `component` intact when the file also has a
`service` column (the real collector output does)."""
from __future__ import annotations

import pandas as pd
from homelab.evaluate import _population, load_tables

FAULT_HEADER = "fault_id,start,end,component,description,company,fault_type,service,status\n"


def _write(tmp_path, statuses):
    collected = tmp_path / "collected"
    collected.mkdir()
    (collected / "alerts.csv").write_text(
        "alert_id,timestamp,source,alert_type,severity,message,company\n"
        "a1,2026-10-22T10:01:00Z,acme/frontend,HomelabCpuHigh,warning,cpu high,acme\n"
        "a2,2026-10-22T11:01:00Z,acme/frontend,HomelabCpuHigh,warning,cpu high,acme\n"
    )
    rows = FAULT_HEADER
    for i, status in enumerate(statuses):
        rows += (
            f"f{i},2026-10-22T1{i}:00:00Z,2026-10-22T1{i}:05:00Z,acme/frontend,cpu-stress on frontend ({status}),"
            f"acme,cpu-stress,frontend,{status}\n"
        )
    faults = tmp_path / "faults.csv"
    faults.write_text(rows)
    return collected, faults


def test_fault_component_survives_load_tables(tmp_path):
    collected, faults = _write(tmp_path, ["closed"])
    _alerts, _metrics, frame = load_tables(collected, faults)
    assert list(frame["component"]) == ["acme/frontend"]


def test_alerts_align_to_faults_and_only_closed_faults_score(tmp_path):
    collected, faults = _write(tmp_path, ["closed", "not-injected"])
    alerts, metrics, frame = load_tables(collected, faults)
    _m, alert_objs, fault_objs = _population(alerts, metrics, frame, "acme")
    assert [f.fault_id for f in fault_objs] == ["f0"]
    assert [a.fault_id for a in alert_objs] == ["f0", None]
    assert isinstance(pd.Timestamp(fault_objs[0].start), pd.Timestamp)


def test_unrecovered_faults_are_not_scored(tmp_path):
    collected, faults = _write(tmp_path, ["unrecovered", "closed"])
    alerts, metrics, frame = load_tables(collected, faults)
    _m, _a, fault_objs = _population(alerts, metrics, frame, "acme")
    assert [f.fault_id for f in fault_objs] == ["f1"]
