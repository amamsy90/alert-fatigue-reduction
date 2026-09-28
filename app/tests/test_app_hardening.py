"""Upload cap, budget validation, one-job-at-a-time, job and evaluation retention."""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SHADOW_MODE", "1")
os.environ.setdefault("QUEUE_DB", str(Path(tempfile.mkdtemp()) / "hardening.db"))

from product.store import QueueStore  # noqa: E402
from src.entities import Action, Alert, Decision  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    monkeypatch.setattr("product.web.evaluate_routes.EVAL_DIR", tmp_path / "evals")
    return TestClient(app_mod.app)


def test_upload_above_limit_is_refused(client, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "0.001")  # 1 KB
    big = b"timestamp,value,service\n" + b"2026-01-01T00:00:00Z,1,web\n" * 200
    r = client.post("/jobs/upload", files={"metrics_file": ("m.csv", big, "text/csv")})
    assert r.status_code == 413


@pytest.mark.parametrize("bad", ["abc", 0, -3, 99999])
def test_bad_budget_is_a_400_not_a_crash(client, bad):
    r = client.post("/jobs/run", json={"mode": "sample", "budget_k": bad})
    assert r.status_code in (400, 422)
    r = client.post("/ingest/alerts?budget_k=abc", json={"alerts": [
        {"timestamp": "2026-01-01T00:00:00Z", "source": "web", "severity": "info", "message": "x"}]})
    assert r.status_code == 400
    r = client.post("/evaluate", data={"dataset": "shop", "budgets": "5,x"})
    assert r.status_code == 400


def _job(store: QueueStore, n: int = 1) -> str:
    import pandas as pd

    alerts = [Alert(f"a{i}", pd.Timestamp("2026-01-01T00:00:00Z"), "web", "t", "info", "m") for i in range(n)]
    decisions = [Decision(a.alert_id, Action.SUPPRESS, 0.1, "r") for a in alerts]
    return store.save_job(source="test", budget_k=5, escalate_threshold=0.5, review_threshold=0.2,
                          shadow_mode=True, alerts=alerts, decisions=decisions, spike_flags=[False] * n)


def test_prune_keeps_newest_jobs_and_all_marks(tmp_path):
    store = QueueStore(tmp_path / "q.db")
    ids = []
    for _ in range(5):
        ids.append(_job(store))
        time.sleep(0.002)  # created_at must differ
    first_item = store.get_queue(ids[0], action="all", limit=1)[0]["id"]
    store.set_feedback("alert", first_item, "real")
    assert store.prune_jobs(keep=2) == 3
    assert store.latest_job_id() == ids[-1]
    assert store.job_meta(ids[0]) is None
    assert store.get_queue(ids[0], action="all", limit=10) == []
    assert len(store.feedback_faults()) == 1  # the mark outlived its job


def test_jobs_run_one_at_a_time(monkeypatch):
    import product.pipeline as pipeline

    active = {"now": 0, "max": 0}
    outcomes = {"ok": 0, "busy": 0}

    def slow(*args, **kwargs):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        active["now"] -= 1
        return {}

    def attempt():
        try:
            pipeline.run_job()
            outcomes["ok"] += 1
        except pipeline.JobBusy:
            outcomes["busy"] += 1

    monkeypatch.setattr(pipeline, "_run_job_locked", slow)
    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1
    assert outcomes["ok"] == 1
    assert outcomes["busy"] == 3


def test_old_evaluations_are_pruned(client, tmp_path):
    from product.web import evaluate_routes as ev

    for name in ("a1", "b2", "c3"):
        (ev.EVAL_DIR / name).mkdir(parents=True)
        time.sleep(0.002)
    ev._prune_evals(keep="c3", limit=1)
    assert sorted(f.name for f in ev.EVAL_DIR.iterdir()) == ["c3"]
