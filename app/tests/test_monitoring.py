"""Production monitoring: /metrics in Prometheus format and /monitoring health checks."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from product import monitoring
from product.monitoring import METRICS, health, route_label
from product.store import QueueStore
from src.entities import Action, Alert, Decision


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    for name in ("APP_PASSWORD", "ALLOW_OPEN_ACCESS", "INGEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    METRICS.reset()
    return TestClient(app_mod.app)


def test_ids_are_collapsed_so_labels_stay_few():
    assert route_label("/models/v0003/activate") == "/models/{id}/activate"
    assert route_label("/evaluate/0a1b2c3d4e5f/score") == "/evaluate/{id}/score"
    assert route_label("/queue") == "/queue"


def test_requests_jobs_and_ingest_are_counted(client):
    client.get("/connectors")
    client.get("/queue")  # 404: no jobs yet
    client.post("/jobs/run", json={"mode": "sample", "budget_k": 20})
    row = {"timestamp": "2026-02-01T10:00:00Z", "source": "web", "severity": "error", "message": "x"}
    client.post("/ingest/alerts", json={"alerts": [row]})
    client.post("/ingest/alerts", json={"alerts": [row]})  # duplicate
    text = client.get("/metrics").text
    assert 'oncall_http_requests_total{method="GET",route="/connectors",status="2xx"} 1' in text
    assert 'oncall_http_requests_total{method="GET",route="/queue",status="4xx"} 1' in text
    assert 'oncall_jobs_total{mode="sample",outcome="ok"} 1' in text
    assert 'oncall_ingest_alerts_total{result="duplicate"} 1' in text
    assert "oncall_http_request_duration_seconds_bucket" in text
    assert "oncall_health_ok" in text
    assert "/metrics" not in text.split("oncall_http_requests_total")[-1]  # scrapes are not counted


def test_monitoring_reports_every_check(client):
    client.get("/connectors")
    body = client.get("/monitoring").json()
    assert set(body["checks"]) == {"latency", "errors", "precision_from_marks", "alert_volume_drift",
                                   "monitoring_gaps", "paging"}
    assert body["checks"]["precision_from_marks"]["status"] == "n/a"  # no marks yet: not judged
    assert body["status"] in ("ok", "warn")


def _save_ingest_job(store: QueueStore, n_alerts: int) -> None:
    t = "2026-02-01T10:00:00Z"
    import pandas as pd

    alerts = [Alert(f"a{i}", pd.Timestamp(t), "web", "log", "info", f"m{i}") for i in range(n_alerts)]
    store.save_job(source="ingest", budget_k=20, escalate_threshold=1.0, review_threshold=1.0, shadow_mode=True,
                   alerts=alerts, decisions=[Decision(a.alert_id, Action.SUPPRESS, 0.1, "") for a in alerts],
                   spike_flags=[False] * n_alerts, meta={"alert_count": n_alerts, "gap_items": 0})


def test_alert_volume_drift_warns(tmp_path):
    store = QueueStore(tmp_path / "q.db")
    for n in (10, 11, 9, 10):
        _save_ingest_job(store, n)
    assert health(store)["checks"]["alert_volume_drift"]["status"] == "ok"
    _save_ingest_job(store, 50)  # five times the usual batch
    report = health(store)
    assert report["checks"]["alert_volume_drift"]["status"] == "warn"
    assert report["status"] == "warn"


def test_low_precision_from_marks_warns(tmp_path, monkeypatch):
    store = QueueStore(tmp_path / "q.db")
    fake = {"real": 5, "noise": 20, "alert": {"precision": 0.2}, "jump": {"precision": None}}
    monkeypatch.setattr(store, "feedback_summary", lambda: fake)
    check = health(store)["checks"]["precision_from_marks"]
    assert check == {"status": "warn", "value": 0.2}
    assert monitoring.TARGETS["precision_from_marks_min"] == 0.7
