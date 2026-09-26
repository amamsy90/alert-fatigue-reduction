"""The CLI turns a plain export into a queue csv with no labels and no server."""
import pandas as pd

from product.cli import main

T0 = pd.Timestamp("2026-03-02", tz="UTC")


def test_cli_scores_an_export_without_labels(tmp_path):
    minutes = list(range(0, 180))
    metrics = pd.DataFrame({
        "timestamp": [T0 + pd.Timedelta(minutes=m) for m in minutes],
        "value": [40.0 if not 100 <= m < 106 else 70.0 for m in minutes],
        "service": "checkout",
    })
    alerts = pd.DataFrame({
        "alert_id": ["a1", "a2"],
        "timestamp": [T0 + pd.Timedelta(minutes=10), T0 + pd.Timedelta(minutes=101)],
        "source": ["checkout", "checkout"],
        "alert_type": ["log", "log"],
        "severity": ["error", "info"],
        "message": ["payment timeout", "request served"],
    })
    metrics.to_csv(tmp_path / "m.csv", index=False)
    alerts.to_csv(tmp_path / "a.csv", index=False)
    out = tmp_path / "q.csv"
    code = main(["score", "--alerts", str(tmp_path / "a.csv"), "--metrics", str(tmp_path / "m.csv"),
                 "--out", str(out), "--jump-mode", "relative", "--jump-ratio", "0.3"])
    assert code == 0
    queue = pd.read_csv(out)
    assert list(queue["kind"]) == ["alert:escalate", "jump"]
