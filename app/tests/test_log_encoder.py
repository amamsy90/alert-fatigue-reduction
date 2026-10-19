"""Contrastive template encoder. Torch training skips when torch is absent."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from src.classifier import AlertClassifier
from src.config import Config
from src.entities import Alert, Fault
from src.features import build_feature_frame
from src.log_encoder import ContrastiveEncoder, featurize, training_groups
from src.template_features import TEMPLATE_COLUMNS, encoder_width, resolve_encoder

T0 = pd.Timestamp("2026-01-05", tz="UTC")


def _alert(alert_id: str, day: int, message: str, source: str = "db", fault_id: str | None = None) -> Alert:
    return Alert(alert_id, T0 + pd.Timedelta(days=day), source, "log", "error", message, fault_id=fault_id)


def _encoder(dim: int = 4) -> ContrastiveEncoder:
    rng = np.random.default_rng(0)
    state = {
        "w1": rng.normal(size=(8, 64)).astype(np.float32),
        "b1": np.zeros(8, dtype=np.float32),
        "w2": rng.normal(size=(dim, 8)).astype(np.float32),
        "b2": np.zeros(dim, dtype=np.float32),
    }
    return ContrastiveEncoder(state, dim=dim, name=f"contrastive-dim{dim}")


def test_contrastive_is_off_and_the_hash_stays_four_wide(monkeypatch):
    monkeypatch.delenv("LOG_ENCODER", raising=False)
    monkeypatch.setenv("LOG_ENCODER_DIM", "32")
    encoder = resolve_encoder(None)
    assert encoder.name == "hash-v1"
    assert encoder_width(encoder) == 4
    frame = build_feature_frame([_alert("a", 0, "disk full 99")], template_features=True)
    assert [column for column in frame.columns if str(column).startswith("tpl_")] == TEMPLATE_COLUMNS


def test_groups_use_only_the_training_window():
    alerts = [
        _alert("train", 1, "disk full on db", fault_id="f-train"),
        _alert("test", 10, "later unique template", fault_id="f-test"),
        _alert("other", 1, "caller timed out", source="web", fault_id="f-train"),
    ]
    faults = [
        Fault("f-train", T0 + pd.Timedelta(days=1), T0 + pd.Timedelta(days=1, hours=1), "db", "mem"),
        Fault("f-test", T0 + pd.Timedelta(days=10), T0 + pd.Timedelta(days=10, hours=1), "db", "mem"),
    ]
    groups = training_groups(alerts, faults, T0 + pd.Timedelta(days=7))
    templates = [template for template, _group in groups]
    assert any("disk" in template for template in templates)
    assert any("caller" in template for template in templates)
    assert all("later" not in template for template in templates)
    assert all(group == "f-train" for _template, group in groups)


def test_only_the_contrastive_encoder_may_use_another_width():
    wide = _encoder(8)
    assert encoder_width(wide) == 8
    frame = build_feature_frame([_alert("a", 0, "disk full")], template_features=True, encoder=wide)
    assert [f"tpl_h{index}" for index in range(8)] == [column for column in frame.columns if str(column).startswith("tpl_h")]
    again = wide.embed("disk full")
    assert again == wide.embed("disk full 42")
    assert len(again) == 8
    narrow = AlertClassifier(Config(), template_features=True, encoder=_encoder(4))
    assert len(narrow._numeric_columns() or []) == len(AlertClassifier(Config(), template_features=True)._numeric_columns() or [])


def test_encoder_load_refuses_a_bad_archive(tmp_path, monkeypatch):
    from src.log_encoder import load_contrastive_encoder, log_encoder_requested

    monkeypatch.delenv("LOG_ENCODER", raising=False)
    assert log_encoder_requested() is False
    monkeypatch.setenv("LOG_ENCODER", "contrastive")
    assert log_encoder_requested() is True
    short = featurize("x")
    assert short.shape == (64,)
    loaded, reason = load_contrastive_encoder("")
    assert loaded is None and reason is not None and "unset" in reason
    loaded, reason = load_contrastive_encoder(str(tmp_path / "missing.safetensors"))
    assert loaded is None and reason == "missing"
    meta = tmp_path / "encoder.json"
    raw = b"not-json"
    meta.write_bytes(raw)
    meta.with_name("encoder.json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    loaded, reason = load_contrastive_encoder(str(tmp_path))
    assert loaded is None and reason == "bad-format"
    paging = {"format": "oncall-log-encoder-v1", "pages": True, "dim": 4, "weights_sha256": "ab" * 32}
    raw = json.dumps(paging).encode("utf-8")
    meta.write_bytes(raw)
    meta.with_name("encoder.json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    loaded, reason = load_contrastive_encoder(str(tmp_path))
    assert loaded is None and reason == "bad-format"


def test_missing_contrastive_file_falls_back_to_the_hash(monkeypatch):
    monkeypatch.setenv("LOG_ENCODER", "contrastive")
    monkeypatch.setenv("LOG_ENCODER_PATH", "/tmp/no-such-encoder.safetensors")
    monkeypatch.setenv("LOG_ENCODER_SHA256", "ab" * 32)
    encoder = resolve_encoder(None)
    assert encoder.name == "hash-v1"
    from src.template_features import encoder_load_status

    status = encoder_load_status()
    assert status["active"] == "hash-v1"
    assert status["error"]


def test_same_template_is_stable_under_the_numpy_encoder():
    encoder = _encoder(4)
    assert featurize("disk full").shape == (64,)
    assert encoder.embed("disk full") == encoder.embed("disk full")


@pytest.mark.ml
def test_infonce_pulls_a_fault_window_together(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("safetensors")
    from src.log_encoder import save_encoder
    from src.log_encoder_torch import train_state

    groups = [
        ("disk full on database", "f1"),
        ("database disk exhausted", "f1"),
        ("gateway timeout upstream", "f2"),
        ("upstream gateway timed out", "f2"),
    ]
    state = train_state(groups, dim=4, hidden=16, epochs=40, seed=0, device="cpu")
    encoder = ContrastiveEncoder(state, dim=4, name="contrastive-dim4")
    digest = save_encoder(encoder, tmp_path)
    from src.log_encoder import load_contrastive_encoder

    loaded, error = load_contrastive_encoder(str(tmp_path), digest)
    assert error is None and loaded is not None
    assert loaded.dim == 4

    def _cos(left: str, right: str) -> float:
        a = np.asarray(loaded.embed(left))
        b = np.asarray(loaded.embed(right))
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))

    same = _cos("disk full on database", "database disk exhausted")
    other = _cos("disk full on database", "gateway timeout upstream")
    assert same > other
