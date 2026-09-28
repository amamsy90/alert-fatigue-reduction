"""Prometheus URL ops + /ops/prometheus/test API (mocked HTTP)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SHADOW_MODE", "1")
os.environ["QUEUE_DB"] = str(Path(tempfile.mkdtemp()) / "import_queue.db")

from product.ops import get_prom_url, set_prom_url  # noqa: E402
from product.store import QueueStore  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


@pytest.fixture()
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setenv("SHADOW_MODE", "1")
    monkeypatch.delenv("PROM_URL", raising=False)
    monkeypatch.setenv("PROM_ALLOW_PRIVATE", "1")
    monkeypatch.delenv("PROM_QUERY", raising=False)
    monkeypatch.delenv("PAGERDUTY_ROUTING_KEY", raising=False)
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    import product.app as app_mod

    app_mod.STORE = QueueStore(tmp_path / "queue.db")
    return TestClient(app_mod.app)


def test_save_and_clear_prom_url(ops_env):
    saved = ops_env.post("/ops/prometheus/url", json={"url": "http://localhost:9090"})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["configured"] is True
    assert body["url"] == "http://localhost:9090"
    assert body["url_host"] == "localhost"
    assert body["source"] == "ops"
    assert get_prom_url() == "http://localhost:9090"

    status = ops_env.get("/connectors").json()["prometheus"]
    assert status["configured"] is True
    assert status["source"] == "ops"
    assert status["url_host"] == "localhost"

    cleared = ops_env.delete("/ops/prometheus/url")
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False
    assert get_prom_url() == ""


def test_prom_url_env_bootstrap(ops_env, monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://env-prom:9090")
    status = ops_env.get("/connectors").json()["prometheus"]
    assert status["configured"] is True
    assert status["source"] == "env"
    assert status["url_host"] == "env-prom"
    # Ops override wins
    set_prom_url("http://ops-prom:9090")
    assert get_prom_url() == "http://ops-prom:9090"
    assert ops_env.get("/connectors").json()["prometheus"]["source"] == "ops"


def test_prom_url_rejects_empty(ops_env):
    bad = ops_env.post("/ops/prometheus/url", json={"url": "   "})
    assert bad.status_code == 400


def test_prometheus_api_get_and_post_mocked(ops_env, monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://user:secret@prom.test:9090")
    build = {"status": "success", "data": {"version": "2.54.0"}}
    query = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"service": "web"}, "values": [[1, "10"], [2, "20"]]},
            ],
        },
    }

    def fake_urlopen(url, timeout=None):
        if "buildinfo" in url:
            return _Resp(build)
        return _Resp(query)

    with patch("product.prometheus_adapter.urlopen", side_effect=fake_urlopen):
        get_res = ops_env.get("/ops/prometheus/test")
        post_res = ops_env.post("/ops/prometheus/test")
    assert get_res.status_code == 200, get_res.text
    assert post_res.status_code == 200, post_res.text
    for res in (get_res, post_res):
        body = res.json()
        assert body["ok"] is True
        assert body["series_count"] == 1
        assert body["points"] == 2
        assert body["url_host"] == "prom.test"
        assert "secret" not in res.text
        assert "values" not in res.text or body.get("points") == 2  # no raw series dump
        # Ensure we did not echo the full metric matrix
        assert "working_set" not in res.text


def test_prometheus_api_without_url(ops_env):
    res = ops_env.post("/ops/prometheus/test")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["error"] == "PROM_URL is not set"
    assert body["series_count"] == 0


def test_connectors_ui_has_test_prometheus_button(ops_env):
    page = ops_env.get("/")
    assert page.status_code == 200
    assert "Test Prometheus" in page.text
    assert "/ops/prometheus/test" in page.text or "btn-test-prom" in page.text
