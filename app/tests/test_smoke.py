"""End-to-end browser path: login, run the sample day, read the queue."""
from __future__ import annotations

from fastapi.testclient import TestClient

from product.store import QueueStore


def test_login_then_sample_job_then_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.delenv("ALLOW_OPEN_ACCESS", raising=False)
    from product.web.security import reset_login_attempts

    reset_login_attempts()
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    http = TestClient(app_mod.app, base_url="http://localhost")
    assert http.get("/").status_code == 401
    signed = http.post("/login", json={"username": "oncall", "password": "pw"})
    assert signed.status_code == 200
    home = http.get("/")
    assert home.status_code == 200
    assert "<html" in home.text.lower()
    run = http.post("/jobs/run", json={"mode": "sample", "budget_k": 20})
    assert run.status_code == 200
    assert run.json()["job_id"]
    queue = http.get("/queue")
    assert queue.status_code == 200
    body = queue.json()
    assert body["job"]
    assert body["count"] >= 0
    reset_login_attempts()
