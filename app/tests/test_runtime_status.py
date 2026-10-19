"""Phase 4: health fields, LLM metrics, and the optional GPU image.

The default queue stays the sklearn columns. Template-feature drift is a no-op
while LLM_TEMPLATE_FEATURES is unset. Explanations stay off unless LLM_EXPLAIN
is set, and /health does not hash weights in that case.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from product.llm import ExplainError, WeightStatus, annotate_shadow
from product.monitoring import METRICS, health
from product.store import QueueStore
from src.accel import clear_backend_cache, gpu_memory_mib, parse_gpu_memory_mib
from src.classifier import AlertClassifier
from src.config import Config


def test_parse_gpu_memory_sums_numeric_devices():
    assert parse_gpu_memory_mib("100\n[N/A]\n50\n") == 150
    assert parse_gpu_memory_mib("[N/A]\n") is None
    assert parse_gpu_memory_mib("") is None


def test_gpu_memory_is_cached_and_absent_when_nvidia_smi_fails(monkeypatch):
    clear_backend_cache()

    class _Out:
        returncode = 0
        stdout = "100\n50\n"

    calls = {"n": 0}

    def fake_run(*_args, **_kwargs):
        calls["n"] += 1
        return _Out()

    monkeypatch.setattr("src.accel.subprocess.run", fake_run)
    assert gpu_memory_mib() == 150
    assert gpu_memory_mib() == 150
    assert calls["n"] == 1

    clear_backend_cache()

    def missing(*_args, **_kwargs):
        raise OSError("nvidia-smi")

    monkeypatch.setattr("src.accel.subprocess.run", missing)
    assert gpu_memory_mib() is None


def test_health_adds_backend_and_llm_without_hashing_or_gpu_memory(tmp_path, monkeypatch):
    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    for name in (
        "APP_PASSWORD", "ALLOW_OPEN_ACCESS", "INGEST_TOKEN", "LLM_EXPLAIN",
        "LLM_TEMPLATE_FEATURES", "ACCEL_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("src.accel.cuml_usable", lambda: True)
    clear_backend_cache()
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(b"present-but-not-hashed")
    monkeypatch.setenv("LLM_GGUF", str(weights))
    monkeypatch.setattr("product.llm._last_error", None)

    def boom(*_args, **_kwargs):
        raise AssertionError("health must not hash weights while explanations are off")

    monkeypatch.setattr("product.llm.sha256_file", boom)
    monkeypatch.setattr("src.accel.gpu_memory_mib", boom)
    monkeypatch.setattr("product.web.ops_routes.probe_prometheus", boom)

    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    body = TestClient(app_mod.app).get("/health").json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert "shadow_mode" in body
    assert body["accel"]["backend"] == "sklearn"
    assert body["llm"] == {"enabled": False, "weights": "present", "last_error": None}
    assert AlertClassifier(Config()).template_features is False


def test_hash_ok_is_reported_when_the_pin_matches(tmp_path, monkeypatch):
    path = tmp_path / "weights.gguf"
    path.write_bytes(b"x")
    monkeypatch.setenv("LLM_EXPLAIN", "1")
    monkeypatch.setenv("LLM_GGUF", str(path))
    monkeypatch.setattr("product.llm.weight_status", lambda: WeightStatus("ok", path, "ab" * 32))
    from product.llm import llm_health

    assert llm_health()["weights"] == "hash-ok"
    assert llm_health()["enabled"] is True


def test_health_reports_hash_mismatch_only_when_explain_is_on(tmp_path, monkeypatch):
    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("LLM_EXPLAIN", "1")
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(b"not-the-pinned-model")
    monkeypatch.setenv("LLM_GGUF", str(weights))
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    body = TestClient(app_mod.app).get("/health").json()
    assert body["llm"]["enabled"] is True
    assert body["llm"]["weights"] == "hash-mismatch"


def test_missing_weights_count_as_fallback_and_off_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_EXPLAIN", "1")
    monkeypatch.setenv("LLM_GGUF", str(tmp_path / "missing.gguf"))
    METRICS.reset()
    record = annotate_shadow({"items": []}, [], spikes=[], model=None)
    assert record["status"] == "missing"
    assert record["notes"] == []
    assert METRICS.total("oncall_llm_explain_total", lambda lab: lab.get("outcome") == "fallback") == 1
    text = METRICS.render()
    assert "oncall_llm_explain_duration_seconds_bucket" in text
    assert 'oncall_llm_explain_total{outcome="fallback"} 1' in text

    monkeypatch.delenv("LLM_EXPLAIN", raising=False)
    METRICS.reset()
    assert annotate_shadow({"items": [{"dedup_key": "a", "kind": "alert"}]}, [], spikes=[], model=None)["status"] == "off"
    assert METRICS.total("oncall_llm_explain_total") == 0


def test_timeout_is_counted_apart_from_fallback(monkeypatch):
    monkeypatch.setenv("LLM_EXPLAIN", "1")
    monkeypatch.setattr("product.llm.weight_status", lambda: WeightStatus("ok", None, "ab" * 32))

    def boom(_context):
        raise ExplainError("timeout")

    monkeypatch.setattr("product.llm._explain_text", boom)
    METRICS.reset()
    record = annotate_shadow({"items": [{"dedup_key": "a", "kind": "alert"}]}, [], spikes=[], model=None)
    assert record["status"] == "timeout"
    assert record["notes"] == []
    assert METRICS.total("oncall_llm_explain_total", lambda lab: lab.get("outcome") == "timeout") == 1
    assert METRICS.total("oncall_llm_explain_total", lambda lab: lab.get("outcome") == "fallback") == 0
    from product.llm import llm_health

    assert llm_health()["last_error"] == "timeout"


def test_template_feature_drift_skips_when_disabled_and_warns_when_on(tmp_path, monkeypatch):
    store = QueueStore(tmp_path / "q.db")
    for mean in (1.0, 1.0, 1.0, 4.0):
        store.save_job(
            source="ingest",
            budget_k=20,
            escalate_threshold=1.0,
            review_threshold=1.0,
            shadow_mode=True,
            alerts=[],
            decisions=[],
            spike_flags=[],
            meta={"alert_count": 1, "gap_items": 0, "template_prior_mean": mean},
        )
    monkeypatch.delenv("LLM_TEMPLATE_FEATURES", raising=False)
    skipped = health(store)
    assert skipped["checks"]["template_feature_drift"] == {"status": "n/a", "value": None}

    monkeypatch.setenv("LLM_TEMPLATE_FEATURES", "1")
    warned = health(store)
    assert warned["checks"]["template_feature_drift"]["status"] == "warn"
    assert warned["status"] == "warn"


def test_default_image_stays_cpu_and_gpu_profile_is_optional():
    root = Path(__file__).resolve().parents[1]
    default = (root / "Dockerfile").read_text(encoding="utf-8")
    gpu = (root / "Dockerfile.gpu").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert [line for line in default.splitlines() if line.startswith("FROM ")] == ["FROM python:3.12-slim"]
    assert "FROM docker.io/nvidia/cuda:12.6.3-runtime-ubuntu24.04" in gpu
    assert "PROM_ALLOW_PRIVATE=0" in gpu
    assert "SHADOW_MODE=1" in gpu
    assert "profiles: [\"gpu\"]" in compose
    assert "dockerfile: Dockerfile.gpu" in compose
    assert "build: ." in compose
