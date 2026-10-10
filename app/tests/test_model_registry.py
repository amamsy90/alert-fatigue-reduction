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


def _report(fp: int, fn: int) -> dict:
    return {"fp": fp, "fn": fn, "tp": 5, "precision": 0.5, "recall": 0.5}


def test_promotion_gate_compares_held_out_cost_on_the_same_alerts():
    cfg = Config()  # a miss costs 10, a false alarm 1
    assert promotion_decision(None, has_incumbent=False, config=cfg)[0]
    better = {"test": _report(fp=3, fn=1), "incumbent_test": _report(fp=1, fn=2)}  # 13 vs 21
    assert promotion_decision(better, True, cfg) == (True, "held-out cost 13 <= active model's 21")
    worse = {"test": _report(fp=0, fn=3), "incumbent_test": _report(fp=5, fn=1)}  # 30 vs 15
    promote, reason = promotion_decision(worse, True, cfg)
    assert not promote and "kept as a candidate" in reason
    assert not promotion_decision({"test": None}, True, cfg)[0]  # no held-out window: keep the active model


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
