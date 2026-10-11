"""Model versions: every trained model kept with a card, a promotion gate, rollback."""
from __future__ import annotations

import logging

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from product import model_store
from product.model_store import SavedModel, activate_version, load_model, load_registry, register_model, save_model
from product.pipeline import promotion_decision
from product.store import QueueStore
from src.config import Config


def _model(threshold: float) -> SavedModel:
    return SavedModel(None, threshold, 0.1, "real", pd.Timestamp.now("UTC").isoformat(), 50, 0)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    return tmp_path


def test_every_model_gets_a_version_and_a_card(env):
    first = register_model(_model(0.5), {"data_fingerprint": "abc", "gate": "no active model yet"}, promote=True)
    assert first["version"] == "v0001" and first["promoted"]
    assert first["data_fingerprint"] == "abc" and first["code_version"]
    assert load_registry()["active"] == "v0001"
    assert load_model().escalate_threshold == 0.5


def test_a_candidate_is_kept_but_not_activated(env):
    register_model(_model(0.5), {}, promote=True)
    second = register_model(_model(0.9), {"gate": "worse"}, promote=False)
    assert second["version"] == "v0002" and not second["promoted"]
    assert load_registry()["active"] == "v0001"
    assert load_model().escalate_threshold == 0.5  # live batches still use v0001
    assert (env / "models" / "v0002.pkl").exists()


def test_rollback_restores_an_earlier_version_and_refuses_a_changed_file(env):
    register_model(_model(0.5), {}, promote=True)
    register_model(_model(0.7), {}, promote=True)
    assert load_model().escalate_threshold == 0.7
    activate_version("v0001")
    assert load_model().escalate_threshold == 0.5
    assert [h["version"] for h in load_registry()["history"]] == ["v0001", "v0002", "v0001"]
    (env / "models" / "v0002.pkl").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed"):
        activate_version("v0002")
    with pytest.raises(KeyError):
        activate_version("v0099")


def _report(fp: int, fn: int, fault_alerts: int = 6) -> dict:
    return {"fp": fp, "fn": fn, "tp": fault_alerts - fn, "fault_alerts": fault_alerts, "precision": 0.5, "recall": 0.5}


def _queue(caught: int, false: int, caught_without: int, false_without: int, faults: int = 10) -> dict:
    """A queue_check as score_alerts records it (miss = 10, false item = 1)."""
    return {
        "faults": faults, "days": 3,
        "caught_with_model": caught, "false_items_with_model": false,
        "cost_with_model": 10 * (faults - caught) + false,
        "caught_no_log_alerts": caught_without, "false_items_no_log_alerts": false_without,
        "cost_no_log_alerts": 10 * (faults - caught_without) + false_without,
    }


GOOD_QUEUE = _queue(caught=9, false=2, caught_without=7, false_without=1)  # 12 < 31


def test_promotion_gate_compares_held_out_cost_on_the_same_alerts():
    cfg = Config()  # a miss costs 10, a false alarm 1; 6 fault alerts -> 60 with no log alerts
    better = {"test": _report(fp=3, fn=1), "incumbent_test": _report(fp=1, fn=2), "queue_check": GOOD_QUEUE}
    promote, reason = promotion_decision(better, True, cfg)  # 13 vs 21
    assert promote and reason.startswith("held-out cost 13 <= active model's 21; queue with its alerts: 9/10")
    worse = {"test": _report(fp=0, fn=3), "incumbent_test": _report(fp=5, fn=1), "queue_check": GOOD_QUEUE}
    promote, reason = promotion_decision(worse, True, cfg)  # 30 vs 15
    assert not promote and "kept as a candidate" in reason
    assert not promotion_decision({"test": None}, True, cfg)[0]  # no held-out window: keep the active model


def test_the_first_model_is_not_promoted_just_because_it_is_first():
    cfg = Config()
    assert promotion_decision(None, has_incumbent=False, config=cfg) == (
        False, "no held-out window to test on; rules only stays")
    # 10 x 1 miss + 55 false alarms = 65, not below 60 for showing no log alerts.
    noisy = {"test": _report(fp=55, fn=1), "queue_check": GOOD_QUEUE}
    promote, reason = promotion_decision(noisy, has_incumbent=False, config=cfg)
    assert not promote and "showing no log alerts" in reason and "rules only stays" in reason
    useful = {"test": _report(fp=8, fn=1), "queue_check": GOOD_QUEUE}  # 18 < 60, queue 12 < 31
    promote, reason = promotion_decision(useful, has_incumbent=False, config=cfg)
    assert promote and reason.startswith("held-out cost 18 < 60 for showing no log alerts")
    no_faults = {"test": _report(fp=8, fn=1), "queue_check": None}
    assert promotion_decision(no_faults, False, cfg) == (
        False, "no held-out fault to test the whole queue on; kept as a candidate, rules only stays")


def test_a_model_right_about_alerts_but_bad_for_the_queue_is_not_promoted():
    """The GAIA case: the classifier finds most fault-bearing alerts (alert cost far
    below showing none), but the queue with its alerts catches fewer faults."""
    cfg = Config()
    gaia_like = {
        "test": _report(fp=8, fn=2),  # 28 < 60: good at the alert level
        "queue_check": _queue(caught=8, false=3, caught_without=9, false_without=1),  # 23 >= 11
    }
    promote, reason = promotion_decision(gaia_like, has_incumbent=False, config=cfg)
    assert not promote
    assert "queue with its alerts: 8/10" in reason and "without log alerts: 9/10" in reason
    assert reason.endswith("kept as a candidate, rules only stays")
    tie = {"test": _report(fp=8, fn=2), "queue_check": _queue(caught=9, false=1, caught_without=9, false_without=1)}
    assert not promotion_decision(tie, False, cfg)[0]  # must earn its slots, not just tie


def test_a_model_that_beats_the_active_one_must_still_beat_no_log_alerts():
    cfg = Config()
    both_bad = {"test": _report(fp=70, fn=0), "incumbent_test": _report(fp=90, fn=0), "queue_check": GOOD_QUEUE}
    promote, reason = promotion_decision(both_bad, True, cfg)  # 70 < 90 but >= 60
    assert not promote and "no log alerts" in reason
    nothing_to_find = {"test": _report(fp=0, fn=0, fault_alerts=0), "queue_check": GOOD_QUEUE}
    assert not promotion_decision(nothing_to_find, False, cfg)[0]


@pytest.fixture()
def client(env, monkeypatch):
    for name in ("QUEUE_DB", "SETTINGS_PATH", "OPS_PATH"):
        monkeypatch.setenv(name, str(env / name.lower()))
    for name in ("APP_PASSWORD", "ALLOW_OPEN_ACCESS"):
        monkeypatch.delenv(name, raising=False)
    import product.app as app_mod

    app_mod.use_store(QueueStore(env / "queue.db"))
    return TestClient(app_mod.app)


def _labelled_upload() -> dict:
    rows = []
    for day in range(10):
        for i in range(12):
            source = "webservice1" if i < 3 else "dbservice1"
            rows.append(f"a{day:02d}{i:02d},2026-01-{day + 1:02d}T12:{i:02d}:00Z,{source},log,warning,event {day}-{i}")
    alerts = "alert_id,timestamp,source,alert_type,severity,message\n" + "\n".join(rows) + "\n"
    faults = "fault_id,start,end,component,description\n" + "".join(
        f"f{day},2026-01-{day + 1:02d}T11:58:00Z,2026-01-{day + 1:02d}T12:03:00Z,webservice1,x\n" for day in range(10)
    )
    return {"alerts_file": ("alerts.csv", alerts, "text/csv"), "faults_file": ("faults.csv", faults, "text/csv")}


def test_training_through_the_app_registers_versions_with_cards(client):
    first = client.post("/jobs/upload", files=_labelled_upload(), data={"budget_k": "20"}).json()
    training = first["meta"]["training"]
    assert training["model_version"] == "v0001" and training["promoted"]
    check = training["queue_check"]  # the whole held-out queue, with and without log alerts
    assert check["faults"] > 0 and check["caught_with_model"] > check["caught_no_log_alerts"]
    assert check["cost_with_model"] < check["cost_no_log_alerts"]
    again = client.post("/jobs/upload", files=_labelled_upload(), data={"budget_k": "20"}).json()["meta"]["training"]
    assert again["model_version"] == "v0002"
    assert again["incumbent_test"] is not None  # the active model was scored on the same held-out alerts
    listing = client.get("/models").json()
    assert [v["version"] for v in listing["versions"]] == ["v0001", "v0002"]
    card = listing["versions"][0]
    assert card["data_fingerprint"] and card["held_out"]["alerts"] > 0 and card["gate"]
    assert client.get("/model").json()["version"] == listing["active"]


def test_rollback_through_the_api_is_audited(client, caplog):
    client.post("/jobs/upload", files=_labelled_upload(), data={"budget_k": "20"})
    register_model(_model(0.99), {}, promote=True)
    assert client.get("/models").json()["active"] == "v0002"
    with caplog.at_level(logging.INFO, logger="oncall.audit"):
        res = client.post("/models/v0001/activate")
    assert res.status_code == 200 and res.json()["active"] == "v0001"
    assert client.post("/models/v0042/activate").status_code == 404
    assert client.post("/models/latest/activate").status_code == 400
    assert any("POST /models/v0001/activate status=200" in r.getMessage() for r in caplog.records)


def test_model_store_paths_follow_model_path(env):
    assert model_store.models_dir() == env / "models"


def test_saved_model_roundtrip_scores_the_same(env):
    import numpy as np

    from src.classifier import AlertClassifier
    from src.entities import Alert

    origin = pd.Timestamp("2026-01-01", tz="UTC")
    alerts = [
        Alert(
            f"a{i}",
            origin + pd.Timedelta(hours=i),
            "web" if i % 2 == 0 else "db",
            "log",
            "error" if i % 3 == 0 else "info",
            f"memory fail {i % 2}" if i % 2 == 0 else f"routine check {i % 2}",
            "f" if i % 2 == 0 else None,
        )
        for i in range(12)
    ]
    fitted = AlertClassifier().fit(alerts)
    save_path = env / "round.pkl"
    save_model(
        SavedModel(fitted, 0.4, 0.2, "real", origin.isoformat(), len(alerts), 1),
        save_path,
    )
    loaded = load_model(save_path)
    assert loaded is not None and loaded.model is not None
    assert loaded.escalate_threshold == 0.4
    assert np.allclose(fitted.predict_proba(alerts), loaded.model.predict_proba(alerts))


def test_pickle_bytes_are_refused_even_with_a_matching_hash(env):
    import hashlib
    import pickle

    path = env / "planted.pkl"
    path.write_bytes(pickle.dumps({"run": "code"}))
    (env / "planted.pkl.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n", encoding="ascii")
    assert load_model(path) is None


def test_corrupt_registry_is_not_treated_as_empty(env):
    registry = model_store.models_dir()
    registry.mkdir()
    (registry / "registry.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        load_registry()


def test_corrupt_registry_returns_503_and_reset_quarantines_the_file(client, env):
    models = model_store.models_dir()
    models.mkdir(parents=True, exist_ok=True)
    (models / "registry.json").write_text("{", encoding="utf-8")
    refused = client.get("/models")
    assert refused.status_code == 503
    assert "RESET" in refused.json()["detail"]
    bad = client.post("/models/reset", json={"confirm": "nope"})
    assert bad.status_code == 400
    ok = client.post("/models/reset", json={"confirm": "RESET"})
    assert ok.status_code == 200
    assert ok.json()["active"] is None
    listed = client.get("/models")
    assert listed.status_code == 200
    assert listed.json()["versions"] == []
    broken = list(models.glob("registry.json.broken-*"))
    assert len(broken) == 1


@pytest.mark.parametrize(
    "meta",
    [
        ["not", "a", "dict"],
        {"format": "oncall-model-v1", "has_pipeline": False},
        {"format": "oncall-model-v1", "has_pipeline": False, "escalate_threshold": None},
    ],
)
def test_malformed_model_metadata_is_refused_not_raised(env, meta):
    import hashlib
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("meta.json", json.dumps(meta))
    path = env / "bad.pkl"
    path.write_bytes(buf.getvalue())
    (env / "bad.pkl.sha256").write_text(hashlib.sha256(buf.getvalue()).hexdigest() + "\n", encoding="ascii")
    assert load_model(path) is None


def test_a_failed_save_keeps_the_previous_model_loadable(env, monkeypatch):
    """The model and its SHA-256 are written to temp files first, so a failure
    part-way through never leaves a model whose digest does not match."""
    save_model(_model(0.5))
    real_write = model_store._write_temp
    calls = {"n": 0}

    def flaky(target, data):
        calls["n"] += 1
        if calls["n"] == 2:  # the digest file
            raise OSError("disk full")
        return real_write(target, data)

    monkeypatch.setattr(model_store, "_write_temp", flaky)
    with pytest.raises(OSError):
        save_model(_model(0.9))
    assert load_model().escalate_threshold == 0.5
    assert not list(env.glob("*.tmp"))


def test_concurrent_saves_leave_a_matching_digest(env):
    import threading

    threads = [threading.Thread(target=save_model, args=(_model(t / 10),)) for t in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    loaded = load_model()
    assert loaded is not None
    assert not list(env.glob("*.tmp"))


def test_a_model_that_fails_the_gate_does_not_score_the_batch(client, monkeypatch):
    """Kept as a candidate; the batch is scored by rules only, and nothing becomes active."""
    import product.pipeline as pipeline

    monkeypatch.setattr(pipeline, "promotion_decision", lambda *a, **k: (False, "test gate says no"))
    job = client.post("/jobs/upload", files=_labelled_upload(), data={"budget_k": "20"}).json()
    meta = job["meta"]
    assert meta["model_used"] == "rules_only"
    training = meta["training"]
    assert training["model_used"] == "candidate" and training["promoted"] is False
    assert training["gate"] == "test gate says no" and training["test"]["alerts"] > 0
    assert training["model_version"] == "v0001"
    listing = client.get("/models").json()
    assert [v["version"] for v in listing["versions"]] == ["v0001"]
    assert listing["active"] is None
    assert client.get("/model").json() == {"saved": False}
    # Every alert in the upload is a warning, so rules only escalates none of them.
    assert meta["escalate"] == 0 and meta["review"] == 0
    items = client.get("/queue", params={"job_id": job["job_id"], "limit": 2000}).json()["items"]
    assert items and {i["reason"] for i in items} == {"severity 'warning' below threshold"}
