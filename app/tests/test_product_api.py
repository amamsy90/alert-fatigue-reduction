"""Product API smoke tests — sample job returns escalate/ask rows at K=20."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure imports resolve when pytest runs from app/
os.environ.setdefault("SHADOW_MODE", "1")
# Import-time store lives in the OS temp folder, not in the repo; each test gets its own.
os.environ["QUEUE_DB"] = str(Path(tempfile.mkdtemp()) / "import_queue.db")

from product.app import app  # noqa: E402
from product.store import QueueStore  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "queue.db"
    monkeypatch.setenv("QUEUE_DB", str(db))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    # Re-bind store used by the app
    import product.app as app_mod

    app_mod.STORE = QueueStore(db)
    return TestClient(app_mod.app)


def test_connectors_dashboard_hides_secrets(client, monkeypatch):
    monkeypatch.setenv("PAGERDUTY_ROUTING_KEY", "secret-key-must-not-appear")
    monkeypatch.setenv("PROM_URL", "http://prom.test:9090")
    monkeypatch.setattr(
        "product.app.probe_prometheus",
        lambda url=None: {"reachable": True, "version": "2.54.0"},
    )
    res = client.get("/connectors")
    assert res.status_code == 200
    body = res.json()
    assert body["prometheus"]["configured"] is True
    assert body["prometheus"]["url"] == "http://prom.test:9090"
    assert body["prometheus"]["probe"] == {"reachable": True, "version": "2.54.0"}
    assert body["pagerduty"]["configured"] is True
    assert body["pagerduty"]["will_page"] is False
    assert "secret-key-must-not-appear" not in res.text
    assert "webhook" in body and body["webhook"]["path"] == "/ingest/alertmanager"
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Connectors" in page.text
    assert "secret-key-must-not-appear" not in page.text


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["shadow_mode"] is True


def test_login_guards_everything_but_health(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    assert client.get("/health").status_code == 200
    denied = client.get("/")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"].startswith("Basic")
    assert client.post("/ingest/alerts", json={"alerts": []}).status_code == 401
    assert client.get("/", auth=("oncall", "wrong")).status_code == 401
    assert client.get("/", auth=("oncall", "s3cret")).status_code == 200


def test_feedback_becomes_real_labels_for_the_next_upload(client):
    rows = []
    for day in range(10):
        for i in range(15):
            rows.append(
                {
                    "timestamp": f"2026-01-{day + 1:02d}T12:{i:02d}:00Z",
                    "source": "webservice1" if i < 3 else "dbservice1",
                    "severity": "warning",
                    "message": f"event {day}-{i}",
                }
            )
    header = "alert_id,timestamp,source,alert_type,severity,message\n"
    csv_text = header + "".join(
        f"a{n},{r['timestamp']},{r['source']},warning,{r['severity']},{r['message']}\n"
        for n, r in enumerate(rows)
    )
    first = client.post(
        "/jobs/upload",
        files={"alerts_file": ("alerts.csv", csv_text, "text/csv")},
        data={"budget_k": "20"},
    )
    # No labels yet: the batch is still queued, on rules only (no model is trained).
    assert first.status_code == 200, first.text
    assert first.json()["meta"]["model_used"] == "rules_only"
    assert first.json()["meta"]["alert_count"] == len(rows)

    sample = client.post("/jobs/run", json={"mode": "sample", "budget_k": 20}).json()
    items = client.get("/queue", params={"job_id": sample["job_id"]}).json()["items"]
    target = items[0]
    ok = client.post("/feedback", json={"kind": "alert", "id": target["id"], "verdict": "real"})
    assert ok.status_code == 200
    noise = client.post("/feedback", json={"kind": "alert", "id": items[1]["id"], "verdict": "noise"})
    assert noise.status_code == 200
    q = client.get("/queue", params={"job_id": sample["job_id"]}).json()
    assert (q["feedback"]["real"], q["feedback"]["noise"]) == (1, 1)
    assert q["items"][0]["verdict"] == "real"
    assert client.post("/feedback", json={"kind": "jump", "id": 999999, "verdict": "real"}).status_code == 404

    exported = client.get("/feedback/faults.csv").text.strip().splitlines()
    assert exported[0] == "fault_id,start,end,component,description"
    assert len(exported) == 2 and target["source"] in exported[1]

    # Confirmed windows for webservice1 on the upload's days make it trainable.
    faults_csv = "fault_id,start,end,component,description\n" + "".join(
        f"fb{day},2026-01-{day + 1:02d}T11:58:00Z,2026-01-{day + 1:02d}T12:03:00Z,webservice1,x\n"
        for day in range(10)
    )
    second = client.post(
        "/jobs/upload",
        files={
            "alerts_file": ("alerts.csv", csv_text, "text/csv"),
            "faults_file": ("faults.csv", faults_csv, "text/csv"),
        },
        data={"budget_k": "20"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["meta"]["label_source"] == "real"


def _labelled_batch_csv() -> tuple[str, str]:
    """Ten days with both fault and noise alerts, so a model can be fit and tested."""
    rows = []
    for day in range(10):
        for i in range(12):
            source = "webservice1" if i < 3 else "dbservice1"
            rows.append(
                f"a{day:02d}{i:02d},2026-01-{day + 1:02d}T12:{i:02d}:00Z,{source},log,warning,event {day}-{i}"
            )
    alerts = "alert_id,timestamp,source,alert_type,severity,message\n" + "\n".join(rows) + "\n"
    faults = "fault_id,start,end,component,description\n" + "".join(
        f"f{day},2026-01-{day + 1:02d}T11:58:00Z,2026-01-{day + 1:02d}T12:03:00Z,webservice1,x\n"
        for day in range(10)
    )
    return alerts, faults


def test_small_live_batch_is_scored_with_the_saved_model(client):
    assert client.get("/model").json() == {"saved": False}
    few = [
        {"timestamp": "2026-02-01T10:00:00Z", "source": "web-01", "severity": "warning", "message": "slow"},
        {"timestamp": "2026-02-01T10:01:00Z", "source": "db-01", "severity": "error", "message": "timeout"},
    ]
    # Too few to train on and nothing saved yet: rules only, nothing is lost.
    early = client.post("/ingest/alerts", json={"alerts": few, "score_now": True})
    assert early.status_code == 200, early.text
    assert early.json()["job"]["meta"]["model_used"] == "rules_only"

    # Run sample is a one-day metric demo and does not fit a classifier.
    # A labelled multi-day upload is what saves the model.
    alerts_csv, faults_csv = _labelled_batch_csv()
    trained = client.post(
        "/jobs/upload",
        files={
            "alerts_file": ("alerts.csv", alerts_csv, "text/csv"),
            "faults_file": ("faults.csv", faults_csv, "text/csv"),
        },
        data={"budget_k": "20"},
    )
    assert trained.status_code == 200, trained.text
    trained = trained.json()
    assert trained["meta"]["model_used"] == "trained"
    report = trained["meta"]["training"]
    assert report["model_used"] == "trained"
    assert report["train"]["alerts"] > 0
    assert report["test"]["alerts"] > 0
    assert report["train"]["start"] < report["test"]["start"]
    status = client.get("/model").json()
    assert status["saved"] and status["label_source"] == "real" and not status["retrain_due"]

    later = [dict(row, timestamp=row["timestamp"].replace("T10:", "T11:")) for row in few]
    res = client.post("/ingest/alerts", json={"alerts": later, "score_now": True})
    assert res.status_code == 200, res.text
    meta = res.json()["job"]["meta"]
    assert meta["model_used"] == "saved"
    assert meta["alert_count"] == 2


def test_resent_alerts_are_buffered_once(client):
    """A retry after 409, or Alertmanager re-sending, must not page twice."""
    row = {"timestamp": "2026-02-01T10:00:00Z", "source": "web-01", "severity": "error", "message": "x"}
    first = client.post("/ingest/alerts", json={"alerts": [row]}).json()
    assert first["duplicates"] == 0 and first["buffered_batches"] == 1
    again = client.post("/ingest/alerts", json={"alerts": [dict(reversed(list(row.items())))]}).json()
    assert again["duplicates"] == 1 and again["buffered_batches"] == 1
    other = client.post("/ingest/alerts", json={"alerts": [dict(row, message="y")]}).json()
    assert other["duplicates"] == 0 and other["buffered_batches"] == 2


def test_oversized_ingest_body_is_refused(client, monkeypatch):
    monkeypatch.setenv("MAX_INGEST_MB", "0.001")  # about 1 KB
    big = [{"timestamp": "2026-02-01T10:00:00Z", "source": "s", "message": "x" * 2000}]
    res = client.post("/ingest/alerts", json={"alerts": big})
    assert res.status_code == 413

    def chunks():  # no Content-Length: counted as it streams
        yield b'{"alerts": ['
        for _ in range(50):
            yield b'{"timestamp": "2026-02-01T10:00:00Z", "source": "s", "message": "xxxxxxxxxx"},'
        yield b"{}]}"

    streamed = client.post("/ingest/alerts", content=chunks(), headers={"content-type": "application/json"})
    assert streamed.status_code == 413


def test_marks_label_the_next_run_and_trigger_a_retrain_reminder(client):
    import pandas as pd

    from product.model_store import load_model
    from product.pipeline import apply_feedback, load_alerts_from_dicts

    # Sample mode is a one-day rules-only demo and does not save a model.
    # Marks are taken from a labelled upload, which does.
    alerts_csv, faults_csv = _labelled_batch_csv()
    trained = client.post(
        "/jobs/upload",
        files={
            "alerts_file": ("alerts.csv", alerts_csv, "text/csv"),
            "faults_file": ("faults.csv", faults_csv, "text/csv"),
        },
        data={"budget_k": "20"},
    )
    assert trained.status_code == 200, trained.text
    assert trained.json()["meta"]["model_used"] == "trained"
    items = client.get("/queue", params={"job_id": trained.json()["job_id"], "limit": 2000}).json()["items"]
    noise_item = next(i for i in items if i["action"] == "suppress")
    real_item = next(i for i in items if i["id"] != noise_item["id"])
    client.post("/feedback", json={"kind": "alert", "id": real_item["id"], "verdict": "real"})
    client.post("/feedback", json={"kind": "alert", "id": noise_item["id"], "verdict": "noise"})

    import product.app as app_mod

    ts = pd.Timestamp(real_item["timestamp"])
    batch = load_alerts_from_dicts(
        [
            {"timestamp": (ts + pd.Timedelta(minutes=2)).isoformat(), "source": real_item["source"],
             "severity": "info", "message": "nearby"},
            {"timestamp": noise_item["timestamp"], "source": noise_item["source"],
             "severity": "info", "message": "marked noise", "fault_id": "f-old"},
        ]
    )
    stats = apply_feedback(batch, app_mod.STORE)
    by_message = {a.message: a for a in batch}
    assert by_message["nearby"].fault_id.startswith("fb_alert_")
    assert by_message["marked noise"].fault_id is None
    assert stats == {"labelled_by_real_marks": 1, "cleared_by_noise_marks": 1}

    status = load_model().status(feedback_real_now=1)
    assert status["retrain_due"] and "1 new Real marks since training" in status["retrain_reasons"]
    old = load_model().status(0, now=pd.Timestamp.now("UTC") + pd.Timedelta(days=8))
    assert old["retrain_due"] and "days old" in old["retrain_reasons"][0]

    summary = client.get("/queue").json()["feedback"]
    assert summary["alert"] == {"real": 1, "noise": 1, "precision": 0.5}


def test_jumps_are_queue_items_capped_per_day(tmp_path):
    import pandas as pd

    from product.pipeline import _jump_items
    from src.entities import Action, Alert, Decision
    from src.memory_spikes import Spike

    t0 = pd.Timestamp("2026-01-10 08:00", tz="UTC")
    spikes = [
        Spike("web", "webservice1", t0, t0 + pd.Timedelta(minutes=5), 1.4e9, rise_at_confirm=1.2e9),
        Spike("db", "dbservice1", t0 + pd.Timedelta(hours=1), t0 + pd.Timedelta(hours=1, minutes=3), 0.7e9,
              rise_at_confirm=0.7e9),
        Spike("db", "dbservice1", t0 + pd.Timedelta(hours=2), t0 + pd.Timedelta(hours=2, minutes=3), 3.0e9,
              rise_at_confirm=0.6e9),
    ]
    alert = Alert("a1", t0 + pd.Timedelta(minutes=2), "webservice1", "error", "error", "oom")
    items = _jump_items(spikes, [alert], [Decision("a1", Action.ESCALATE, 0.9, "")])
    assert items[0]["rise_gb"] == 1.2
    assert items[0]["alerts_inside"] == 1 and items[0]["escalated_inside"] == 1
    assert items[1]["alerts_inside"] == 0

    store = QueueStore(tmp_path / "q.db")
    job_id = store.save_job(
        source="test",
        budget_k=2,
        escalate_threshold=0.5,
        review_threshold=0.1,
        shadow_mode=True,
        alerts=[alert],
        decisions=[Decision("a1", Action.ESCALATE, 0.9, "")],
        spike_flags=[True],
        jumps=items,
    )
    shown, total = store.get_jumps(job_id, per_day=2)
    assert total == 3
    # First confirmed first; the later, bigger peak does not jump the queue.
    assert [j["rise_gb"] for j in shown] == [1.2, 0.7]


def test_sample_job_queue_has_surfaced_actions(client):
    res = client.post("/jobs/run", json={"mode": "sample", "budget_k": 20})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["job_id"]
    assert body["meta"]["alert_count"] > 0

    q = client.get("/queue", params={"job_id": body["job_id"], "action": "escalate"})
    assert q.status_code == 200
    # Sample data usually escalates some; review may also fill budget
    q_all = client.get("/queue", params={"job_id": body["job_id"]})
    items = q_all.json()["items"]
    actions = {i["action"] for i in items}
    assert "escalate" in actions or "review" in actions
    surfaced = [i for i in items if i["action"] in ("escalate", "review")]
    assert len(surfaced) > 0


def test_sample_job_includes_jump_gap_and_leak(client, monkeypatch, tmp_path):
    """Run sample is one synthetic day with metrics, not alerts alone."""
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("LEAK_WINDOW_MINUTES", raising=False)
    res = client.post("/jobs/run", json={"mode": "sample", "budget_k": 20})
    assert res.status_code == 200, res.text
    meta = res.json()["meta"]
    assert meta["has_metrics"] is True
    assert meta["jumps_shown"] >= 1
    assert meta["gap_items"] >= 1
    assert meta["leak_items"] >= 1
    assert meta["detector"] == "built-in memory-jump rule"
    assert meta["model_used"] == "rules_only"

    # The escalate filter must not drop metric items: they are a separate list.
    q = client.get("/queue", params={"job_id": res.json()["job_id"], "action": "escalate"}).json()
    kinds = {j["kind"] for j in q["jumps"]}
    assert {"jump", "gap", "leak"} <= kinds
    assert q["items"] and all(i["action"] == "escalate" for i in q["items"])
    jump = next(j for j in q["jumps"] if j["kind"] == "jump")
    assert jump["alerts_inside"] >= 1
    assert jump["shown"] == 1

    page = client.get("/")
    assert page.status_code == 200
    for label in ("Memory jump", "Monitoring gap", "Slow leak", "metric-kind"):
        assert label in page.text


def test_sample_job_skips_leaks_when_the_rule_is_off(client, monkeypatch):
    monkeypatch.setenv("LEAK_WINDOW_MINUTES", "0")
    res = client.post("/jobs/run", json={"mode": "sample", "budget_k": 20})
    assert res.status_code == 200, res.text
    meta = res.json()["meta"]
    assert meta["has_metrics"] is True
    assert meta["jumps_shown"] >= 1
    assert meta["gap_items"] >= 1
    assert meta["leak_items"] == 0


def test_alertmanager_webhook_is_buffered(client):
    body = {
        "version": "4",
        "status": "firing",
        "receiver": "oncall-queue",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighMemory", "service": "webservice1", "severity": "warning"},
                "annotations": {"summary": "memory jump"},
                "startsAt": "2026-01-12T10:00:00Z",
                "fingerprint": "fp1",
            }
        ],
    }
    res = client.post("/ingest/alertmanager", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["received"] == 1
    assert "job" not in res.json()


def test_ingest_webhook_score_now(client):
    # Enough alerts spanning days with mixed severity for weak/demo training
    alerts = []
    for day in range(10):
        for i in range(15):
            sev = "error" if i < 2 else "info"
            alerts.append(
                {
                    "timestamp": f"2026-01-{day + 1:02d}T12:{i:02d}:00Z",
                    "source": "webservice1",
                    "severity": sev,
                    "alert_type": sev,
                    "message": f"event {day}-{i} {'fault window' if sev == 'error' else 'noise'}",
                    "fault_id": "f1" if sev == "error" else None,
                }
            )
    res = client.post(
        "/ingest/alerts",
        json={"alerts": alerts, "score_now": True, "budget_k": 20},
    )
    assert res.status_code == 200, res.text
    assert "job" in res.json()
    job_id = res.json()["job"]["job_id"]
    q = client.get("/queue", params={"job_id": job_id})
    assert q.status_code == 200
    assert q.json()["count"] > 0


def test_every_alert_of_a_batch_is_in_the_queue(client):
    """Regression: the early (training) half of a batch used to be dropped."""
    import pandas as pd

    job = client.post("/jobs/run", json={"mode": "sample", "budget_k": 20}).json()
    from product.pipeline import _default_paths

    raw = pd.read_csv(_default_paths()["sample_dir"] / "alerts.csv")
    assert job["meta"]["alert_count"] == len(raw)
    assert len(job["days"]) == pd.to_datetime(raw["timestamp"]).dt.date.nunique()


def test_failed_scoring_keeps_the_ingest_buffer(client, monkeypatch):
    import product.app as app_module

    def boom(*args, **kwargs):
        raise ValueError("scoring failed")

    monkeypatch.setattr(app_module, "run_job", boom)
    row = {"timestamp": "2026-02-01T10:00:00Z", "source": "web-01", "severity": "error", "message": "x"}
    res = client.post("/ingest/alerts", json={"alerts": [row], "score_now": True})
    assert res.status_code == 400
    assert app_module.STORE.buffer_stats()["buffered_batches"] == 1


def test_ingest_token(client, monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "s3cret")
    row = {"timestamp": "2026-02-01T10:00:00Z", "source": "web-01", "severity": "error", "message": "x"}
    assert client.post("/ingest/alerts", json={"alerts": [row]}).status_code == 401
    ok = client.post("/ingest/alerts", json={"alerts": [row]}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    # The token opens only the ingest paths.
    monkeypatch.setenv("APP_PASSWORD", "pw")
    assert client.get("/queue", headers={"Authorization": "Bearer s3cret"}).status_code == 401


def test_tampered_model_file_is_refused(tmp_path):
    import pandas as pd

    from product.model_store import SavedModel, load_model, save_model

    path = tmp_path / "model.pkl"
    save_model(SavedModel(None, 0.5, 0.1, "real", pd.Timestamp.now("UTC").isoformat(), 10, 0), path)
    assert load_model(path).escalate_threshold == 0.5
    path.write_bytes(path.read_bytes() + b"x")
    assert load_model(path) is None


def test_no_password_means_private_networks_only(monkeypatch):
    from product.app import _is_private

    assert _is_private("127.0.0.1") and _is_private("172.17.0.1") and _is_private("10.2.3.4")
    assert not _is_private("8.8.8.8")
    public = TestClient(app, client=("8.8.8.8", 5000))
    assert public.get("/connectors").status_code == 403
    assert public.get("/health").status_code == 200
    monkeypatch.setenv("ALLOW_OPEN_ACCESS", "1")
    assert public.get("/health").status_code == 200


def test_noise_mark_matches_the_same_instant_in_another_format(tmp_path):
    import pandas as pd

    from product.pipeline import apply_feedback
    from src.entities import Action, Alert, Decision

    store = QueueStore(tmp_path / "q.db")
    t = pd.Timestamp("2026-01-05 14:13:08", tz="UTC")
    job = store.save_job(source="t", budget_k=5, escalate_threshold=0.5, review_threshold=0.1,
                         shadow_mode=True, alerts=[Alert("a1", t, "db", "log", "info", "m")],
                         decisions=[Decision("a1", Action.ESCALATE, 0.9, "")], spike_flags=[False])
    item = store.get_queue(job)[0]
    store.set_feedback("alert", item["id"], "noise")
    again = Alert("other-id", pd.Timestamp("2026-01-05T22:13:08+08:00"), "db", "log", "info", "m", "f1")
    apply_feedback([again], store)
    assert again.fault_id is None
