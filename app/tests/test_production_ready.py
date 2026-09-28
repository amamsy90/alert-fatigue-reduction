"""Production hardening: SSRF gate, cheap health, busy job 409, Slack host check."""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SHADOW_MODE", "1")
os.environ.setdefault("QUEUE_DB", str(Path(tempfile.mkdtemp()) / "prod.db"))

from product import ops  # noqa: E402
from product.pipeline import JobBusy, run_job  # noqa: E402
from product.store import QueueStore  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setenv("SHADOW_MODE", "1")
    monkeypatch.delenv("PROM_URL", raising=False)
    monkeypatch.delenv("PROM_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("PAGERDUTY_ROUTING_KEY", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    return TestClient(app_mod.app)


def test_health_does_not_probe_prometheus(client, monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom.example:9090")
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("health must not probe Prometheus")

    monkeypatch.setattr("product.web.ops_routes.probe_prometheus", boom)
    monkeypatch.setattr("product.prometheus_adapter.probe_prometheus", boom)
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert "shadow_mode" in body
    assert called["n"] == 0


def test_metadata_prometheus_url_always_refused(client, monkeypatch):
    monkeypatch.setenv("PROM_ALLOW_PRIVATE", "1")
    for bad in (
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[fe80::1]:9090",
    ):
        r = client.post("/ops/prometheus/url", json={"url": bad})
        assert r.status_code == 400, bad


def test_private_prometheus_needs_env_override(client, monkeypatch):
    monkeypatch.delenv("PROM_ALLOW_PRIVATE", raising=False)
    denied = client.post("/ops/prometheus/url", json={"url": "http://10.0.0.5:9090"})
    assert denied.status_code == 400
    assert "PROM_ALLOW_PRIVATE" in denied.json()["detail"]

    monkeypatch.setenv("PROM_ALLOW_PRIVATE", "1")
    ok = client.post("/ops/prometheus/url", json={"url": "http://10.0.0.5:9090"})
    assert ok.status_code == 200
    assert ok.json()["configured"] is True
    assert ok.json()["url_host"] == "10.0.0.5"


def test_fetch_respects_ssrf_gate(monkeypatch):
    monkeypatch.delenv("PROM_ALLOW_PRIVATE", raising=False)
    monkeypatch.setenv("PROM_URL", "http://127.0.0.1:9090")
    from product.prometheus_adapter import fetch_memory_metrics, probe_prometheus

    with pytest.raises(ValueError, match="PROM_ALLOW_PRIVATE"):
        fetch_memory_metrics()
    out = probe_prometheus()
    assert out["reachable"] is False
    assert "PROM_ALLOW_PRIVATE" in out["error"]


def test_slack_webhook_must_be_hooks_slack(client):
    bad = client.post("/ops/slack/webhook", json={"webhook_url": "https://evil.example/hooks"})
    assert bad.status_code == 400
    ok = client.post(
        "/ops/slack/webhook",
        json={"webhook_url": "https://hooks.slack.com/services/T00/B00/xxx"},
    )
    assert ok.status_code == 200
    assert ok.json()["configured"] is True
    body = client.get("/connectors")
    assert body.status_code == 200
    assert "hooks.slack.com/services" not in body.text
    assert body.json()["slack"]["last4"] == "/xxx"


def test_busy_job_returns_409(client, monkeypatch):
    import product.app as app_mod
    import product.pipeline as pipeline

    held = threading.Event()
    release = threading.Event()

    def slow(*args, **kwargs):
        held.set()
        release.wait(timeout=5)
        return {"job_id": "x", "meta": {}}

    monkeypatch.setattr(pipeline, "_run_job_locked", slow)

    def hold():
        run_job(app_mod.STORE, mode="sample", budget_k=5)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(timeout=2)
    with pytest.raises(JobBusy):
        run_job(app_mod.STORE, mode="sample", budget_k=5)
    r = client.post("/jobs/run", json={"mode": "sample", "budget_k": 5})
    assert r.status_code == 409
    release.set()
    t.join(timeout=2)


def test_ops_file_chmod_best_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    ops.set_routing_key("rk_secret_abcd")
    path = tmp_path / "ops.json"
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    assert "rk_secret_abcd" in raw  # stored for the app
    # On Unix the mode should be owner-only; on Windows just ensure save worked.
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
