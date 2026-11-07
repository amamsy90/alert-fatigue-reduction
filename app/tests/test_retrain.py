"""Scheduled retraining: the policy comes from the written rules, the rest runs through the app."""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import product.retrain as retrain
from product.model_store import SavedModel, load_model, load_registry
from product.retrain import MIN_HOURS_BETWEEN, MIN_NEW_REAL_MARKS, retrain_decision, run_retrain
from product.store import QueueStore

NOW = pd.Timestamp("2026-03-20 12:00", tz="UTC")


def _saved(age_days: float, real_marks: int = 0) -> SavedModel:
    return SavedModel(None, 0.5, 0.1, "real", (NOW - pd.Timedelta(days=age_days)).isoformat(), 50, real_marks)


def _registry(hours_ago: float | None) -> dict:
    if hours_ago is None:
        return {"active": None, "versions": []}
    return {"active": "v0001", "versions": [{"trained_at": (NOW - pd.Timedelta(hours=hours_ago)).isoformat()}]}


# --- the policy (docs/mlops.md) ------------------------------------------------------------


def test_nothing_new_is_not_due():
    assert not retrain_decision(_saved(1), 0, _registry(48), now=NOW)["due"]


def test_enough_new_real_marks_make_it_due():
    d = retrain_decision(_saved(1, real_marks=2), 2 + MIN_NEW_REAL_MARKS, _registry(48), now=NOW)
    assert d["due"] and "new Real marks" in d["reasons"][0]
    assert not retrain_decision(_saved(1, 2), 2 + MIN_NEW_REAL_MARKS - 1, _registry(48), now=NOW)["due"]


def test_an_old_model_needs_at_least_one_new_mark():
    assert retrain_decision(_saved(8), 1, _registry(48), now=NOW)["due"]
    assert not retrain_decision(_saved(8), 0, _registry(48), now=NOW)["due"]  # old but nothing to learn from
    assert not retrain_decision(_saved(6), 1, _registry(48), now=NOW)["due"]  # a mark, but not old and not enough


def test_no_model_yet_needs_the_minimum_marks():
    assert retrain_decision(None, MIN_NEW_REAL_MARKS, _registry(None), now=NOW)["due"]
    assert not retrain_decision(None, MIN_NEW_REAL_MARKS - 1, _registry(None), now=NOW)["due"]


def test_cooldown_blocks_even_a_forced_retrain():
    d = retrain_decision(_saved(20), 10, _registry(MIN_HOURS_BETWEEN - 1), now=NOW, force=True)
    assert not d["due"] and d["blockers"]
    assert retrain_decision(_saved(20), 10, _registry(MIN_HOURS_BETWEEN + 1), now=NOW, force=True)["due"]


# --- through the app -----------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    for name in ("APP_PASSWORD", "ALLOW_OPEN_ACCESS"):
        monkeypatch.delenv(name, raising=False)
    import product.app as app_mod

    store = QueueStore(tmp_path / "queue.db")
    app_mod.use_store(store)
    return store, TestClient(app_mod.app)


def _later() -> pd.Timestamp:
    return pd.Timestamp.now("UTC") + pd.Timedelta(hours=MIN_HOURS_BETWEEN + 1)


def _history_with_marks(store, client, marks: int) -> None:
    """One upload job gives the history; `marks` Real marks and as many Noise marks label it."""
    from tests.test_model_registry import _labelled_upload

    client.post("/jobs/upload", files=_labelled_upload(), data={"budget_k": "20"})
    with store._connect() as conn:
        real = [r["id"] for r in conn.execute("SELECT id FROM decisions WHERE source='webservice1' ORDER BY id")]
        noise = [r["id"] for r in conn.execute("SELECT id FROM decisions WHERE source='dbservice1' ORDER BY id")]
    for item in real[:marks]:
        store.set_feedback("alert", item, "real")
    for item in noise[:marks]:
        store.set_feedback("alert", item, "noise")


def test_not_due_without_marks_runs_nothing(env):
    store, client = env
    _history_with_marks(store, client, 0)
    report = run_retrain(store, now=_later())
    assert report["ran"] is False and "not due" in report["reason"]
    assert load_registry()["versions"][-1]["version"] == "v0001"  # nothing new was trained


def test_a_due_retrain_writes_a_full_model_card(env):
    store, client = env
    _history_with_marks(store, client, MIN_NEW_REAL_MARKS + 1)
    report = run_retrain(store, trigger="scheduled", now=_later())
    assert report["ran"] is True, report
    card = next(v for v in load_registry()["versions"] if v["version"] == report["version"])
    assert card["trigger"] == "scheduled"
    assert card["retrain"]["reasons"] and card["retrain"]["history_alerts"] > 0
    assert card["data_fingerprint"] and card["code_version"]
    assert {"python", "scikit-learn", "numpy", "pandas"} <= set(card["environment"])
    assert card["batch_window"]["alerts"] > 0
    if report["promoted"]:
        assert card["verification"]["ok"]


def test_the_cooldown_stops_a_second_retrain_right_away(env):
    store, client = env
    _history_with_marks(store, client, MIN_NEW_REAL_MARKS + 1)
    first = run_retrain(store, force=True, now=_later())
    assert first["ran"] is True
    # The first retrain's model was trained "now" (wall clock); asking again at the same moment is too soon.
    second = run_retrain(store, force=True, now=pd.Timestamp.now("UTC"))
    assert second["ran"] is False and "minimum" in second["reason"]


def test_a_model_that_fails_verification_is_rolled_back(env, monkeypatch):
    store, client = env
    _history_with_marks(store, client, MIN_NEW_REAL_MARKS + 1)
    before = load_registry()["active"]
    assert before == "v0001"
    monkeypatch.setattr("product.pipeline.promotion_decision", lambda *a, **k: (True, "forced promotion"))
    monkeypatch.setattr(retrain, "smoke_test", lambda alerts: (False, "forced failure"))
    report = run_retrain(store, now=_later())
    assert report["promoted"] is True and report["rolled_back"] is True
    assert load_registry()["active"] == before and report["active_after"] == before
    card = next(v for v in load_registry()["versions"] if v["version"] == report["version"])
    assert card["verification"] == {"ok": False, "detail": "forced failure"}
    assert load_model() is not None  # the old model is back and loads


def test_a_scoring_job_in_progress_is_never_interrupted(env):
    store, client = env
    _history_with_marks(store, client, MIN_NEW_REAL_MARKS + 1)
    from product.pipeline import _JOB_LOCK

    with _JOB_LOCK:
        report = run_retrain(store, now=_later())
    assert report["ran"] is False and "running" in report["reason"]


def test_the_real_smoke_test_accepts_the_active_model(env):
    store, client = env
    _history_with_marks(store, client, 0)
    from product.inputs import load_alerts_from_dicts

    ok, detail = retrain.smoke_test(load_alerts_from_dicts(store.history_alerts()))
    assert ok, detail


def test_the_api_runs_the_same_retrain(env):
    store, client = env
    _history_with_marks(store, client, 0)
    body = client.post("/models/retrain", json={}).json()
    assert body["ran"] is False  # not due: no new marks
    body = client.post("/models/retrain", json={"force": True}).json()
    assert body["ran"] is False and "minimum" in body["reason"]  # forced, but the model is minutes old


def test_model_age_and_retrain_due_are_exported(env):
    store, client = env
    _history_with_marks(store, client, 0)
    text = client.get("/metrics").text
    assert "oncall_model_age_days" in text and "oncall_model_retrain_due 0" in text


def test_deactivating_leaves_live_jobs_on_rules_and_keeps_every_version(env):
    store, client = env
    _history_with_marks(store, client, 0)
    from product.model_store import deactivate_active

    deactivate_active()
    assert load_model() is None
    registry = load_registry()
    assert registry["active"] is None and [v["version"] for v in registry["versions"]] == ["v0001"]
    assert registry["history"][-1]["note"] == "deactivated"
