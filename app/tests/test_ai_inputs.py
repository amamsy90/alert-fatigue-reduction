"""AI-specific tests: unexpected, malformed and edge-case input must not break the queue."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from product.inputs import load_alerts_from_dicts
from product.pipeline import score_alerts
from product.store import QueueStore
from src.config import Config
from src.memory_spikes import detect_gaps, detect_jumps


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    for name in ("APP_PASSWORD", "ALLOW_OPEN_ACCESS", "INGEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    return TestClient(app_mod.app)


GOOD = {"timestamp": "2026-01-01T00:00:00Z", "source": "web", "severity": "error", "message": "oom"}


@pytest.mark.parametrize("bad, reason", [
    (1, "not an object"),
    ({"source": "web"}, "unreadable timestamp"),
    ({"timestamp": "yesterday-ish", "source": "web"}, "unreadable timestamp"),
    ({"timestamp": "2026-01-01T00:00:00Z"}, "missing source"),
    ({"timestamp": "2026-01-01T00:00:00Z", "source": "  "}, "missing source"),
])
def test_bad_rows_are_rejected_with_a_reason_and_good_ones_kept(client, bad, reason):
    res = client.post("/ingest/alerts", json={"alerts": [bad, GOOD]})
    assert res.status_code == 200
    body = res.json()
    assert body["received"] == 2 and body["rejected"] == 1
    assert reason in body["errors"][0]["reason"]


def test_a_batch_with_no_usable_alert_is_refused(client):
    res = client.post("/ingest/alerts", json={"alerts": [1, {"source": "x"}]})
    assert res.status_code == 400
    assert res.json()["detail"]["rejected"] == 2


def test_a_bad_row_can_no_longer_block_every_later_job(client):
    """It used to be buffered and make each later score_now fail."""
    client.post("/ingest/alerts", json={"alerts": [{"source": "web"}, GOOD]})
    later = dict(GOOD, timestamp="2026-01-01T01:00:00Z")
    res = client.post("/ingest/alerts", json={"alerts": [later], "score_now": True})
    assert res.status_code == 200, res.text
    assert res.json()["job"]["meta"]["alert_count"] == 2


def test_a_buffer_poisoned_before_validation_still_scores():
    alerts = load_alerts_from_dicts([{"source": "web"}, 7, GOOD, {"timestamp": "nope", "source": "db"}])
    assert [a.source for a in alerts] == ["web"]


def test_unusual_text_is_kept_and_long_messages_are_cut(client):
    odd = dict(GOOD, message="内存 🔥 ümlaut " + "x" * 5000, severity="PANIC!!")
    res = client.post("/ingest/alerts", json={"alerts": [odd], "score_now": True})
    assert res.status_code == 200, res.text
    (item,) = client.get("/queue").json()["items"]
    assert item["message"].startswith("内存 🔥") and len(item["message"]) <= 300
    assert item["action"] == "suppress"  # an unknown severity is not escalated by rules


def test_malformed_json_is_a_400_not_a_crash(client):
    res = client.post("/ingest/alerts", content=b"{nope", headers={"content-type": "application/json"})
    assert res.status_code == 400


def _series(values, freq="30s") -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(values), freq=freq, tz="UTC")
    return pd.DataFrame({"timestamp": idx, "value": values, "service": "web", "series_id": "web"})


def test_metric_edge_cases_do_not_invent_jumps():
    cfg = Config()
    flat = _series(np.full(200, 1e9))
    assert detect_jumps(flat, cfg) == []
    with_nan = _series(np.where(np.arange(200) % 17 == 0, np.nan, 1e9))
    assert detect_jumps(with_nan.dropna(), cfg) == []
    one_point = _series([1e9])
    assert detect_jumps(one_point, cfg) == [] and detect_gaps(one_point, cfg) == []
    shuffled = _series(np.r_[np.full(100, 1e9), np.full(100, 2.5e9)]).sample(frac=1.0, random_state=0)
    assert len(detect_jumps(shuffled, cfg)) == 1  # rows out of order are sorted, the step is found once


def test_labels_of_one_class_fall_back_to_rules():
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    rows = [dict(GOOD, timestamp=(t0 + pd.Timedelta(minutes=i)).isoformat(), fault_id=None) for i in range(30)]
    result = score_alerts(load_alerts_from_dicts(rows), budget_k=5)
    assert result.model_used == "rules_only"
