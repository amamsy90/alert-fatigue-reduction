"""GPU backend requirements (docs/GPU_LLM_ROLES.md, FR-21, FR-22, FR-23).

REQUIREMENTS
- The LLM never changes the queue unless ``LLM_TEMPLATE_FEATURES`` is on.
  That flag is off by default. ``classifier_decisions`` takes alerts, scores
  and config only.
- No output path without a CPU fallback. A missing CUDA device or a missing
  library resolves to sklearn, and a GPU fit that fails is retrained with
  sklearn. The saved pipeline is scikit-learn either way.
- No future or realised-label leakage into LLM prompts (Phase 2a). The
  classifier does not train on the realised label column; a later prompt
  must be built from the same feature frame.

GPU numerical checks are skipped when this machine has no CUDA device.
"""
from __future__ import annotations

import ast
import builtins
import inspect
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.accel import (
    CONVERT_PROB_ATOL,
    LOGISTIC_JACCARD_MIN,
    LOGISTIC_PROB_ATOL,
    NODE_DTYPE,
    TREE_SKOPS_TYPE,
    TreeEnsembleClassifier,
    available_backends,
    base_margin_from_xgb,
    clear_backend_cache,
    cuda_available,
    cuda_version,
    cuml_usable,
    get_backend,
    install_linear_head,
    parse_cuda_version,
    trees_from_xgb_dump,
    xgboost_gpu_params,
    xgboost_gpu_usable,
)
from src.classifier import AlertClassifier, _make_pipeline, classifier_decisions
from src.config import Config
from src.entities import Action, Alert
from src.features import build_feature_frame
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.sample_data import generate

FIXTURE = Path(__file__).parent / "fixtures" / "xgb_booster_dump.json"


def _alerts() -> list[Alert]:
    origin = pd.Timestamp("2026-01-01", tz="UTC")
    return [
        Alert(
            f"a{i}",
            origin + pd.Timedelta(hours=i),
            "web" if i % 2 == 0 else "db",
            "log",
            "error" if i % 3 == 0 else "info",
            "memory fail" if i % 2 == 0 else "routine check",
            "f" if i % 2 == 0 else None,
        )
        for i in range(12)
    ]


def _block_imports(monkeypatch: pytest.MonkeyPatch, blocked: set[str]) -> None:
    real = builtins.__import__

    def fake(name: str, *args, **kwargs):
        root = name.split(".", 1)[0]
        if root in blocked or name in blocked:
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_queue_decisions_have_no_llm_argument():
    """FR-22. An LLM note has nowhere to change escalate versus suppress."""
    assert tuple(inspect.signature(classifier_decisions).parameters) == ("alerts", "proba", "config")


def test_realised_label_is_not_a_model_feature():
    """FR-23. The fault label is for scoring, not for the decision."""
    columns: list[str] = []
    for _name, _trans, cols in _make_pipeline(0).named_steps["features"].transformers:
        columns.extend(cols)
    assert "label" not in columns
    assert "fault_id" not in columns


def test_training_modules_do_not_import_pickle():
    """FR-21. Models stay skops archives. pickle and joblib are not loaders."""
    for module_name in ("accel", "classifier"):
        path = Path(__file__).parents[1] / "src" / f"{module_name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".", 1)[0]]
            else:
                continue
            assert "pickle" not in names
            assert "joblib" not in names


def test_cuda_version_is_parsed_from_nvidia_smi_text():
    text = "| NVIDIA-SMI 550.54.15    Driver Version: 550.54.15    CUDA Version: 12.4     |"
    assert parse_cuda_version(text) == "12.4"
    assert parse_cuda_version("no device") is None


def test_cuda_probe_uses_the_driver_not_a_wheel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.accel._nvidia_smi_text", lambda: None)
    monkeypatch.setattr("src.accel._nvidia_device_node", lambda: False)
    clear_backend_cache()
    assert cuda_available() is False
    clear_backend_cache()
    monkeypatch.setattr(
        "src.accel._nvidia_smi_text",
        lambda: "CUDA Version: 12.4\n",
    )
    assert cuda_available() is True
    assert cuda_version() == "12.4"


def test_missing_gpu_libraries_fall_back_to_sklearn(monkeypatch: pytest.MonkeyPatch, caplog):
    """FR-21. No CUDA library, no cuML, no XGBoost: the backend is sklearn."""
    monkeypatch.delenv("ACCEL_BACKEND", raising=False)
    monkeypatch.setattr("src.accel.cuda_available", lambda: True)
    _block_imports(monkeypatch, {"cuml", "xgboost"})
    caplog.set_level("INFO")
    assert cuml_usable() is False
    assert xgboost_gpu_usable() is False
    assert available_backends()["sklearn"] is True
    assert get_backend() == "sklearn"
    assert get_backend("cuml") == "sklearn"
    assert get_backend("xgboost-gpu") == "sklearn"
    assert get_backend("nope") == "sklearn"
    assert "sklearn" in caplog.text


def test_auto_stays_sklearn_when_cuml_is_usable(monkeypatch: pytest.MonkeyPatch):
    """cuML same-seed fits are not the published queue. GPU is opt-in."""
    monkeypatch.delenv("ACCEL_BACKEND", raising=False)
    monkeypatch.setattr("src.accel.cuml_usable", lambda: True)
    monkeypatch.setattr("src.accel.xgboost_gpu_usable", lambda: True)
    assert get_backend() == "sklearn"
    assert get_backend("auto") == "sklearn"
    assert get_backend("cuml") == "cuml"
    assert get_backend("xgboost-gpu") == "xgboost-gpu"
    monkeypatch.setenv("ACCEL_BACKEND", "cuml")
    assert get_backend() == "cuml"


def test_environment_variable_selects_and_falls_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.accel.xgboost_gpu_usable", lambda: False)
    monkeypatch.setenv("ACCEL_BACKEND", "xgboost-gpu")
    assert get_backend() == "sklearn"
    monkeypatch.setenv("ACCEL_BACKEND", "sklearn")
    assert get_backend() == "sklearn"


def test_missing_gpu_backends_build_the_same_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """FR-21. On a CPU-only machine the named GPU backends match sklearn exactly."""
    if cuml_usable() or xgboost_gpu_usable():
        pytest.skip("CUDA backend is usable; queue agreement is covered by the gpu-marked tests")
    monkeypatch.delenv("ACCEL_BACKEND", raising=False)
    generate(tmp_path, seed=42, days=14)
    alerts = load_alerts(tmp_path / "alerts.csv")
    faults = load_faults(tmp_path / "faults.csv")
    alerts, _faults = align_alerts_to_faults(alerts, faults)
    config = Config(budget_k=5, random_seed=42)
    train, test = time_split(alerts, config.split_day)
    cpu = AlertClassifier(config, backend="sklearn").fit(train)
    cpu_scores = cpu.predict_proba(test)
    cpu_actions = [d.action for d in classifier_decisions(test, cpu_scores, config)]
    for name in ("cuml", "xgboost-gpu"):
        other = AlertClassifier(config, backend=name).fit(train)
        assert other.backend_used == "sklearn"
        assert np.allclose(cpu_scores, other.predict_proba(test))
        actions = [d.action for d in classifier_decisions(test, other.predict_proba(test), config)]
        assert actions == cpu_actions


def test_a_failing_gpu_fit_is_retrained_on_sklearn(monkeypatch: pytest.MonkeyPatch):
    from src.accel import AccelUnavailable

    def boom(*_args, **_kwargs):
        raise AccelUnavailable("device missing")

    monkeypatch.setattr("src.classifier.get_backend", lambda _prefer=None: "cuml")
    monkeypatch.setattr("src.classifier.cuml_logistic_coefficients", boom)
    alerts = _alerts()
    cpu = AlertClassifier(backend="sklearn").fit(alerts)
    failed = AlertClassifier(backend="cuml").fit(alerts)
    assert failed.backend_used == "sklearn"
    assert np.allclose(cpu.predict_proba(alerts), failed.predict_proba(alerts))


def test_copied_coefficients_match_and_load_on_cpu(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Simulated GPU convert path: coefficients in, sklearn pipeline out, skops load."""
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    from product.model_store import SavedModel, load_model, save_model

    alerts = _alerts()
    fitted = AlertClassifier().fit(alerts)
    clf = fitted._pipe.named_steps["clf"]
    blank = AlertClassifier().fit(alerts)
    install_linear_head(blank._pipe.named_steps["clf"], clf.coef_, clf.intercept_, clf.classes_)
    assert np.max(np.abs(fitted.predict_proba(alerts) - blank.predict_proba(alerts))) <= CONVERT_PROB_ATOL
    blank.backend_used = "cuml"
    origin = pd.Timestamp("2026-01-01", tz="UTC")
    path = tmp_path / "converted.pkl"
    save_model(SavedModel(blank, 0.4, 0.2, "real", origin.isoformat(), len(alerts), 0), path)
    loaded = load_model(path)
    assert loaded is not None and loaded.model is not None
    assert loaded.model.backend_used == "cuml"
    assert np.allclose(fitted.predict_proba(alerts), loaded.model.predict_proba(alerts))


def test_two_row_coefficients_reduce_to_the_positive_class():
    clf = _make_pipeline(0).named_steps["clf"]
    coef = np.array([[-0.5, 0.25], [0.5, -0.25]])
    intercept = np.array([-0.1, 0.1])
    install_linear_head(clf, coef, intercept, np.array([0, 1]))
    assert clf.coef_.shape == (1, 2)
    assert np.allclose(clf.coef_, [[1.0, -0.5]])
    assert np.allclose(clf.intercept_, [0.2])


def test_xgb_dump_fixture_matches_the_recorded_probabilities():
    """Tree walk checked against a real XGBoost 3.4.1 dump. No GPU, no xgboost import."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trees, base = trees_from_xgb_dump(payload["dumps"], payload["base_score"])
    head = TreeEnsembleClassifier(trees=trees, base_score=base)
    x = np.asarray(payload["x"], dtype=np.float64)
    y = np.zeros(len(x), dtype=int)
    y[0] = 1
    head.fit(x, y)
    got = head.predict_proba(x)[:, 1]
    assert np.max(np.abs(got - np.asarray(payload["proba"]))) <= CONVERT_PROB_ATOL


def test_tree_head_round_trips_through_the_model_archive(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    from product.model_store import SavedModel, load_model, save_model

    alerts = _alerts()
    model = AlertClassifier().fit(alerts)
    frame = build_feature_frame(alerts, model.config)
    transformed = model._pipe.named_steps["features"].transform(frame)
    nodes = np.zeros(3, dtype=NODE_DTYPE)
    nodes[0] = (0, np.float32(0.0), 1, 2, 1, 0.0)
    nodes[1] = (-1, np.float32(0.0), -1, -1, -1, -1.0)
    nodes[2] = (-1, np.float32(0.0), -1, -1, -1, 1.0)
    head = TreeEnsembleClassifier(trees=[nodes], base_score=0.0).fit(transformed, frame["label"].to_numpy())
    model._pipe.steps[-1] = ("clf", head)
    model.backend_used = "xgboost-gpu"
    origin = pd.Timestamp("2026-01-01", tz="UTC")
    path = tmp_path / "trees.pkl"
    save_model(SavedModel(model, 0.4, 0.2, "real", origin.isoformat(), len(alerts), 0), path)
    loaded = load_model(path)
    assert loaded is not None and loaded.model is not None
    assert loaded.model.backend_used == "xgboost-gpu"
    assert np.allclose(model.predict_proba(alerts), loaded.model.predict_proba(alerts))
    # The allowlist name is the one skops records for this class.
    assert type(loaded.model._pipe.named_steps["clf"]).__module__ + "." + type(
        loaded.model._pipe.named_steps["clf"]
    ).__name__ == TREE_SKOPS_TYPE


def test_a_cyclic_tree_is_refused():
    nodes = np.zeros(1, dtype=NODE_DTYPE)
    nodes[0] = (0, np.float32(1.0e9), 0, 0, 0, 0.0)
    head = TreeEnsembleClassifier(trees=[nodes], base_score=0.0).fit(np.zeros((2, 1)), np.array([0, 1]))
    with pytest.raises(ValueError, match="leaf"):
        head.predict_proba(np.zeros((2, 1)))


def test_xgboost_device_accepts_a_cuda_index():
    """XGBoost 3.4 saves ``cuda:0``. That is a GPU fit. ``cpu`` is not."""
    from src.accel import AccelUnavailable, is_cuda_device, xgboost_gpu_ensemble

    assert is_cuda_device("cuda")
    assert is_cuda_device("cuda:0")
    assert is_cuda_device("CUDA:1")
    assert is_cuda_device("cpu") is False
    assert is_cuda_device("cuda:") is False
    assert is_cuda_device(None) is False

    import sys
    import types

    class _Booster:
        def __init__(self, device: str) -> None:
            self._device = device

        def save_config(self) -> str:
            return json.dumps({"learner": {"generic_param": {"device": self._device}}})

    class _XGB:
        device = "cuda:0"

        def __init__(self, **_kwargs) -> None:
            pass

        def fit(self, *_args, **_kwargs):
            return self

        def get_booster(self) -> _Booster:
            return _Booster(type(self).device)

    class _Head:
        def fit(self, *_args, **_kwargs):
            return self

    fake = types.ModuleType("xgboost")
    fake.XGBClassifier = _XGB
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(sys.modules, "xgboost", fake)
    monkeypatch.setattr("src.accel.ensemble_from_booster", lambda _booster: _Head())
    try:
        x = np.zeros((4, 2))
        y = np.array([0, 1, 0, 1])
        got = xgboost_gpu_ensemble(x, y, np.ones(4), 0)
        assert isinstance(got, _Head)
        _XGB.device = "cpu"
        with pytest.raises(AccelUnavailable, match="not cuda"):
            xgboost_gpu_ensemble(x, y, np.ones(4), 0)
    finally:
        monkeypatch.undo()


def test_base_score_accepts_the_xgboost_config_shapes():
    assert base_margin_from_xgb("[5E-1]") == pytest.approx(0.0)
    assert base_margin_from_xgb(0.5) == pytest.approx(0.0)
    assert base_margin_from_xgb([0.5]) == pytest.approx(0.0)
    assert base_margin_from_xgb("2.5") == pytest.approx(2.5)


def test_model_card_records_the_accelerator(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.delenv("ACCEL_BACKEND", raising=False)
    from product.model_store import SavedModel, register_model

    saved = SavedModel(None, 0.5, 0.1, "real", pd.Timestamp.now("UTC").isoformat(), 4, 0)
    entry = register_model(saved, {}, promote=True)
    accel = entry["accelerator"]
    assert accel["backend"] == "sklearn"
    assert "cuda" in accel
    assert accel["libraries"]["scikit-learn"]
    assert set(accel["available"]) == {"sklearn", "cuml", "xgboost-gpu"}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


@pytest.mark.gpu
@pytest.mark.skipif(not cuml_usable(), reason="cuML and a CUDA device are required")
def test_cuml_and_sklearn_queues_agree_within_tolerance(tmp_path: Path):
    """Fixed seed. Solver tolerance is LOGISTIC_PROB_ATOL and LOGISTIC_JACCARD_MIN."""
    generate(tmp_path, seed=42, days=14)
    alerts = load_alerts(tmp_path / "alerts.csv")
    faults = load_faults(tmp_path / "faults.csv")
    alerts, _faults = align_alerts_to_faults(alerts, faults)
    config = Config(budget_k=5, random_seed=42)
    train, test = time_split(alerts, config.split_day)
    cpu = AlertClassifier(config, backend="sklearn").fit(train)
    gpu = AlertClassifier(config, backend="cuml").fit(train)
    assert gpu.backend_used == "cuml"
    cpu_scores = cpu.predict_proba(test)
    gpu_scores = gpu.predict_proba(test)
    assert float(np.mean(np.abs(cpu_scores - gpu_scores))) <= LOGISTIC_PROB_ATOL
    cpu_ids = {a.alert_id for a, d in zip(test, classifier_decisions(test, cpu_scores, config)) if d.action == Action.ESCALATE}
    gpu_ids = {a.alert_id for a, d in zip(test, classifier_decisions(test, gpu_scores, config)) if d.action == Action.ESCALATE}
    assert _jaccard(cpu_ids, gpu_ids) >= LOGISTIC_JACCARD_MIN


@pytest.mark.gpu
@pytest.mark.skipif(not xgboost_gpu_usable(), reason="XGBoost built with CUDA and a CUDA device are required")
def test_xgboost_gpu_matches_the_converted_cpu_head(tmp_path: Path):
    """The booster and the skops tree head produce the same queue."""
    import xgboost as xgb

    generate(tmp_path, seed=42, days=14)
    alerts = load_alerts(tmp_path / "alerts.csv")
    faults = load_faults(tmp_path / "faults.csv")
    alerts, _faults = align_alerts_to_faults(alerts, faults)
    config = Config(budget_k=5, random_seed=42)
    train, test = time_split(alerts, config.split_day)
    gpu = AlertClassifier(config, backend="xgboost-gpu").fit(train)
    assert gpu.backend_used == "xgboost-gpu"
    frame = build_feature_frame(train, config)
    transformed = gpu._pipe.named_steps["features"].transform(frame)
    booster = xgb.XGBClassifier(**xgboost_gpu_params(config.random_seed))
    weights = np.where(frame["label"].to_numpy() == 1, config.miss_cost, config.false_alarm_cost)
    booster.fit(transformed, frame["label"].to_numpy(), sample_weight=weights)
    from src.accel import ensemble_from_booster

    head = ensemble_from_booster(booster.get_booster())
    head.fit(transformed, frame["label"].to_numpy())
    test_frame = build_feature_frame(test, config)
    test_x = gpu._pipe.named_steps["features"].transform(test_frame)
    assert np.max(np.abs(booster.predict_proba(test_x)[:, 1] - head.predict_proba(test_x)[:, 1])) <= CONVERT_PROB_ATOL
    saved = gpu.predict_proba(test)
    direct = head.predict_proba(test_x)[:, 1]
    # Two CUDA fits with the same seed; the converted head is what was saved.
    assert np.max(np.abs(saved - direct)) <= CONVERT_PROB_ATOL


def test_live_xgboost_dump_matches_when_the_library_is_installed():
    """Converter fidelity on CPU. Skipped in the default install, which has no xgboost."""
    xgb = pytest.importorskip("xgboost")
    rng = np.random.default_rng(0)
    x = rng.normal(size=(30, 3))
    y = (x[:, 0] > 0).astype(int)
    y[0] = 0
    y[1] = 1
    model = xgb.XGBClassifier(
        n_estimators=5,
        max_depth=2,
        learning_rate=0.3,
        objective="binary:logistic",
        tree_method="hist",
        device="cpu",
        random_state=1,
        n_jobs=1,
        verbosity=0,
    )
    model.fit(x, y)
    from src.accel import ensemble_from_booster

    head = ensemble_from_booster(model.get_booster())
    head.fit(x, y)
    assert np.max(np.abs(model.predict_proba(x)[:, 1] - head.predict_proba(x)[:, 1])) <= CONVERT_PROB_ATOL
