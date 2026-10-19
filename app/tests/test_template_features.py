"""Optional template features (FR-22). No GPU library and no embedding weights."""
from __future__ import annotations

import builtins
import hashlib

import numpy as np
import pandas as pd

from product.model_store import SavedModel, load_model, save_model
from run_classifier_ablation import evaluate_template_features
from src.classifier import AlertClassifier, classifier_decisions
from src.config import Config
from src.entities import Alert
from src.features import NUMERIC_COLUMNS, build_feature_frame
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.queue import alert_items, build_queue, n_days_between
from src.sample_data import generate
from src.template_features import TEMPLATE_COLUMNS, resolve_encoder

T0 = pd.Timestamp("2026-01-05 10:00:00", tz="UTC")


def _alert(alert_id: str, minutes: float, message: str, fault_id: str | None = None) -> Alert:
    return Alert(alert_id, T0 + pd.Timedelta(minutes=minutes), "web", "oom", "error", message, fault_id=fault_id)


def test_template_features_use_only_rows_at_or_before_the_alert():
    early = _alert("early", 0, "disk full", "FAULT-SECRET")
    mid = _alert("mid", 1, "cpu warm")
    late = _alert("late", 30, "disk full", "FAULT-LATER")
    full = build_feature_frame([early, mid, late], template_features=True).set_index("alert_id")
    prefix = build_feature_frame([early, mid], template_features=True).set_index("alert_id")
    for column in TEMPLATE_COLUMNS:
        assert full.loc["early", column] == prefix.loc["early", column]
        assert full.loc["mid", column] == prefix.loc["mid", column]
    # The later copy is first in the list. It still must not count for `early`.
    messy = build_feature_frame([late, early], template_features=True).set_index("alert_id")
    assert full.loc["early", "tpl_prior"] == 0
    assert messy.loc["early", "tpl_prior"] == 0
    assert full.loc["late", "tpl_prior"] == 1
    assert "FAULT-SECRET" not in "".join(str(full.loc["early", column]) for column in TEMPLATE_COLUMNS)
    off = build_feature_frame([early, late])
    assert not any(str(column).startswith("tpl_") for column in off.columns)


def test_same_template_has_the_same_hash_whatever_the_label():
    labelled = build_feature_frame([_alert("a", 0, "disk full", "FAULT-SECRET")], template_features=True)
    plain = build_feature_frame([_alert("b", 0, "disk full", None)], template_features=True)
    for column in ("tpl_h0", "tpl_h1", "tpl_h2", "tpl_h3"):
        assert labelled.loc[0, column] == plain.loc[0, column]


def test_default_pipeline_matches_the_baseline_queue(tmp_path, monkeypatch):
    """FR-22. With the flag off, scores and the shown alert queue match the current columns.

    Both fits are pinned to sklearn. ``auto`` is sklearn too; the pin keeps this
    comparison identical on a machine where cuML is installed.
    """
    monkeypatch.delenv("LLM_TEMPLATE_FEATURES", raising=False)
    monkeypatch.setenv("ACCEL_BACKEND", "sklearn")
    generate(tmp_path, seed=42, days=14)
    alerts = load_alerts(tmp_path / "alerts.csv")
    faults = load_faults(tmp_path / "faults.csv")
    alerts, _faults = align_alerts_to_faults(alerts, faults)
    config = Config(budget_k=20)
    train, test = time_split(alerts, config.split_day)
    baseline = AlertClassifier(config, template_features=False, backend="sklearn").fit(train)
    followed = AlertClassifier(config, backend="sklearn").fit(train)
    base_proba = baseline.predict_proba(test)
    assert np.allclose(base_proba, followed.predict_proba(test))
    base_decisions = classifier_decisions(test, base_proba, config)
    followed_decisions = classifier_decisions(test, followed.predict_proba(test), config)
    assert [item.action for item in base_decisions] == [item.action for item in followed_decisions]
    base_queue = [item.alert.alert_id for item in build_queue(alert_items(test, base_decisions), config)]
    followed_queue = [item.alert.alert_id for item in build_queue(alert_items(test, followed_decisions), config)]
    assert followed_queue == base_queue
    numeric = baseline._pipe.named_steps["features"].transformers[0][2]
    assert list(numeric) == list(NUMERIC_COLUMNS)
    assert baseline.template_features is False

    save_model(
        SavedModel(baseline, 0.5, 0.2, "real", "2026-01-01T00:00:00+00:00", len(train), 0),
        tmp_path / "model.pkl",
    )
    monkeypatch.setenv("LLM_TEMPLATE_FEATURES", "1")
    loaded = load_model(tmp_path / "model.pkl")
    assert loaded is not None and loaded.model is not None
    assert loaded.model.template_features is False
    assert np.allclose(loaded.model.predict_proba(test), base_proba)
    forced_off = AlertClassifier(config, template_features=False)
    assert forced_off.template_features is False
    assert AlertClassifier(config).template_features is True


def test_template_ablation_accepts_a_stub_encoder(tmp_path):
    class Stub:
        name = "stub"

        def embed(self, template: str) -> tuple[float, float, float, float]:
            return (float(len(template) % 3), 0.0, 0.0, 1.0)

    generate(tmp_path, seed=1, days=14)
    alerts = load_alerts(tmp_path / "alerts.csv")
    faults = load_faults(tmp_path / "faults.csv")
    alerts, faults = align_alerts_to_faults(alerts, faults)
    config = Config(budget_k=20)
    train, test = time_split(alerts, config.split_day)
    _, test_faults = time_split(faults, config.split_day)
    origin = min(alert.timestamp for alert in alerts)
    boundary = origin + pd.Timedelta(days=config.split_day)
    end = max(alert.timestamp for alert in test) + pd.Timedelta(seconds=1)
    report = evaluate_template_features(
        train,
        test,
        test_faults,
        config,
        n_days=n_days_between(boundary, end, config),
        dataset="synthetic-stub",
        encoder=Stub(),
    )
    delta = report["per_fault_catch_delta"]
    assert report["encoder"] == "stub"
    assert report["split"] == "first week train, later days test"
    assert report["decision"] == ("keep" if delta["low"] > 0 else "drop")
    assert "baseline_detection_rate" in report
    assert "candidate_detection_rate" in report
    assert delta["n"] == len(test_faults)


def test_missing_or_unpinned_sentence_encoder_uses_the_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_TEMPLATE_ENCODER", str(tmp_path / "missing.safetensors"))
    monkeypatch.setenv("LLM_TEMPLATE_ENCODER_SHA256", "ab" * 32)
    assert resolve_encoder().name == "hash-v1"

    payload = b"not-a-model"
    path = tmp_path / "encoder.safetensors"
    path.write_bytes(payload)
    monkeypatch.setenv("LLM_TEMPLATE_ENCODER", str(path))
    monkeypatch.setenv("LLM_TEMPLATE_ENCODER_SHA256", "0" * 64)
    assert resolve_encoder().name == "hash-v1"

    monkeypatch.setenv("LLM_TEMPLATE_ENCODER_SHA256", hashlib.sha256(payload).hexdigest())
    real = builtins.__import__

    def blocked(name: str, *args, **kwargs):
        if name.split(".", 1)[0] == "sentence_transformers":
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert resolve_encoder().name == "hash-v1"
    from src.template_features import encoder_load_status

    assert encoder_load_status()["error"] == "sentence-transformers is not installed"


def test_pinned_sentence_encoder_loads_the_weight_files_folder(tmp_path, monkeypatch):
    """Hash the weight file. Load the model from its folder, not from the file path."""
    import sys
    import types

    folder = tmp_path / "all-MiniLM"
    folder.mkdir()
    weights = folder / "model.safetensors"
    payload = b"weights"
    weights.write_bytes(payload)
    seen: dict[str, str] = {}

    class _Fake:
        def __init__(self, path: str) -> None:
            seen["path"] = path

        def encode(self, texts):
            return np.ones((len(texts), 6))

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _Fake
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setenv("LLM_TEMPLATE_ENCODER", str(weights))
    monkeypatch.setenv("LLM_TEMPLATE_ENCODER_SHA256", hashlib.sha256(payload).hexdigest())
    encoder = resolve_encoder()
    assert seen["path"] == str(folder)
    assert encoder.name.startswith("sentence-transformers@")
    assert len(encoder.embed("disk full")) == 4
    from src.template_features import encoder_load_status

    status = encoder_load_status()
    assert status["active"] == encoder.name
    assert status["error"] is None
    assert status["requested"] == str(weights)


def test_a_failed_sentence_encoder_load_is_recorded(tmp_path, monkeypatch, caplog):
    import sys
    import types

    folder = tmp_path / "model"
    folder.mkdir()
    weights = folder / "model.safetensors"
    payload = b"weights"
    weights.write_bytes(payload)

    class _Boom:
        def __init__(self, path: str) -> None:
            raise NotADirectoryError(path)

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _Boom
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setenv("LLM_TEMPLATE_ENCODER", str(weights))
    monkeypatch.setenv("LLM_TEMPLATE_ENCODER_SHA256", hashlib.sha256(payload).hexdigest())
    caplog.set_level("ERROR")
    assert resolve_encoder().name == "hash-v1"
    from src.template_features import encoder_load_status

    error = encoder_load_status()["error"] or ""
    assert "NotADirectoryError" in error
    assert "fell back to hash-v1" in caplog.text

    monkeypatch.setenv("LLM_TEMPLATE_ENCODER", str(folder))
    assert resolve_encoder().name == "hash-v1"
    assert encoder_load_status()["error"] == "weights file missing"
