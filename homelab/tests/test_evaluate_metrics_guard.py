"""A metrics table that stops long before the alerts is a partial read."""
from pathlib import Path

import pandas as pd
import pytest
from homelab import collect
from homelab.evaluate import load_tables


def _write(tmp: Path, metric_end: str) -> tuple[Path, Path]:
    collected = tmp / "collected"
    collected.mkdir()
    pd.DataFrame(
        [
            {"alert_id": "a1", "timestamp": "2026-10-21T00:00:00Z", "source": "acme/cart", "alert_type": "X", "severity": "warning", "message": "m", "company": "acme"},
            {"alert_id": "a2", "timestamp": "2026-10-30T15:00:00Z", "source": "acme/cart", "alert_type": "X", "severity": "warning", "message": "m", "company": "acme"},
        ]
    ).to_csv(collected / "alerts.csv", index=False)
    pd.DataFrame(
        [
            {"timestamp": "2026-10-21T00:00:00Z", "value": 1.0, "service": "acme/cart", "company": "acme", "metric": "memory_working_set_bytes"},
            {"timestamp": metric_end, "value": 1.0, "service": "acme/cart", "company": "acme", "metric": "memory_working_set_bytes"},
        ]
    ).to_csv(collected / "metrics.csv", index=False)
    faults = tmp / "faults.csv"
    pd.DataFrame(
        [{"fault_id": "f1", "start": "2026-10-22T00:00:00Z", "end": "2026-10-22T00:03:00Z", "component": "cart", "description": "d", "company": "acme", "fault_type": "pod-kill", "service": "cart", "status": "closed"}]
    ).to_csv(faults, index=False)
    return collected, faults


def test_truncated_metrics_refused(tmp_path):
    collected, faults = _write(tmp_path, "2026-10-26T06:08:14Z")
    with pytest.raises(SystemExit, match="partly read"):
        load_tables(collected, faults)


def test_current_metrics_accepted(tmp_path):
    collected, faults = _write(tmp_path, "2026-10-30T14:59:00Z")
    alerts, metrics, _ = load_tables(collected, faults)
    assert len(alerts) == 2 and len(metrics) == 2


def test_write_table_replaces_atomically(tmp_path):
    path = tmp_path / "t.csv"
    collect.write_table(path, [{"k": 1, "v": "a"}], ("k", "v"), ["k"])
    added = collect.write_table(path, [{"k": 2, "v": "b"}], ("k", "v"), ["k"])
    assert added == 1
    assert list(pd.read_csv(path)["k"]) == [1, 2]
    assert not (tmp_path / ".t.csv.tmp").exists()
