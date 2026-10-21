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


def test_monitoring_reports_every_check(client, monkeypatch):
    monkeypatch.delenv("LLM_TEMPLATE_FEATURES", raising=False)
    client.get("/connectors")
    body = client.get("/monitoring").json()
    assert set(body["checks"]) == {"latency", "errors", "precision_from_marks", "alert_volume_drift",
                                   "metric_value_drift", "template_feature_drift", "monitoring_gaps", "paging"}
    assert body["checks"]["template_feature_drift"]["status"] == "n/a"
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


def test_metric_median_drift_warns(tmp_path):
    store = QueueStore(tmp_path / "q.db")
    t = "2026-02-01T10:00:00Z"
    import pandas as pd

    def _job(median: float) -> None:
        alerts = [Alert("a0", pd.Timestamp(t), "web", "log", "info", "m")]
        store.save_job(
            source="ingest",
            budget_k=20,
            escalate_threshold=1.0,
            review_threshold=1.0,
            shadow_mode=True,
            alerts=alerts,
            decisions=[Decision(alerts[0].alert_id, Action.SUPPRESS, 0.1, "")],
            spike_flags=[False],
            meta={"alert_count": 1, "gap_items": 0, "metric_median": median},
        )

    for med in (100.0, 110.0, 90.0, 100.0):
        _job(med)
    assert health(store)["checks"]["metric_value_drift"]["status"] == "ok"
    _job(500.0)
    report = health(store)
    assert report["checks"]["metric_value_drift"]["status"] == "warn"
    assert report["status"] == "warn"


def test_low_precision_from_marks_warns(tmp_path, monkeypatch):
    store = QueueStore(tmp_path / "q.db")
    fake = {"real": 5, "noise": 20, "alert": {"precision": 0.2}, "jump": {"precision": None}}
    monkeypatch.setattr(store, "feedback_summary", lambda: fake)
    check = health(store)["checks"]["precision_from_marks"]
    assert check == {"status": "warn", "value": 0.2}
    assert monitoring.TARGETS["precision_from_marks_min"] == 0.7


def _medians_job(store, medians: dict[str, float]) -> None:
    import pandas as pd

    alerts = [Alert("a0", pd.Timestamp("2026-02-01T10:00:00Z"), "web", "log", "info", "m")]
    store.save_job(
        source="ingest", budget_k=20, escalate_threshold=1.0, review_threshold=1.0, shadow_mode=True,
        alerts=alerts, decisions=[Decision(alerts[0].alert_id, Action.SUPPRESS, 0.1, "")], spike_flags=[False],
        meta={"alert_count": 1, "gap_items": 0, "metric_median": 100.0, "metric_medians": medians},
    )


def test_one_series_changing_scale_warns_when_the_fleet_median_does_not(tmp_path):
    """Requirement: a single service whose metric moves outside 0.5-2x of its own
    history is reported, even when the fleet-wide median stays the same."""
    store = QueueStore(tmp_path / "q.db")
    fleet = {f"svc{i}": 100.0 for i in range(9)}
    for _ in range(4):
        _medians_job(store, {**fleet, "db": 100.0})
    assert health(store)["checks"]["metric_value_drift"]["status"] == "ok"
    _medians_job(store, {**fleet, "db": 400.0})
    report = health(store)
    assert report["checks"]["metric_value_drift"]["status"] == "warn"
    assert report["data"]["metric_drift_series"] == ["db"]


def test_a_series_dropping_to_zero_warns(tmp_path):
    store = QueueStore(tmp_path / "q.db")
    for _ in range(4):
        _medians_job(store, {"web": 100.0, "db": 100.0})
    _medians_job(store, {"web": 100.0, "db": 0.0})
    assert health(store)["checks"]["metric_value_drift"]["status"] == "warn"


def test_recent_series_medians_use_the_last_hour_of_each_series():
    import pandas as pd

    from product.pipeline import _recent_series_medians

    t = pd.date_range("2026-02-01", periods=24 * 60, freq="min", tz="UTC")
    old = pd.DataFrame({"timestamp": t, "value": 100.0, "series_id": "db"})
    old.loc[old.index[-40:], "value"] = 400.0  # most of the last hour moved up 4x
    medians = _recent_series_medians(old)
    assert medians["db"] == 400.0


def test_jobs_saved_in_the_same_clock_tick_keep_their_order(tmp_path, monkeypatch):
    """Requirement: the newest job is the one saved last, even when two jobs share a timestamp."""
    import product.store as store_mod

    store = QueueStore(tmp_path / "q.db")
    fixed = "2026-02-01T10:00:00+00:00"
    monkeypatch.setattr(store_mod, "pd_utcnow", lambda: fixed)
    for n in (1, 2, 3):
        _save_ingest_job(store, n)
    assert [j["meta"]["alert_count"] for j in store.recent_jobs()] == [3, 2, 1]
