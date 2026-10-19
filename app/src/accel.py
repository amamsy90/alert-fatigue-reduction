"""Optional GPU training backends. The saved classifier is always scikit-learn.

`get_backend` returns ``cuml``, ``xgboost-gpu`` or ``sklearn``. A missing
library or a missing CUDA device falls back to sklearn and is logged. Nothing
in this module is imported from a GPU package unless a caller asks for that
backend, and nothing is written with pickle.

The queue's default head is logistic regression. ``cuml`` trains that same
model and copies coefficients into ``LogisticRegression`` so ``load_model``
stays a CPU skops load. ``xgboost-gpu`` is a different model (a tree booster).
It is used only when requested, and the booster is walked into
``TreeEnsembleClassifier`` before anything is saved: skops cannot store an
``xgboost.Booster``. See ``docs/GPU_LLM_ROLES.md``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.linear_model import LogisticRegression

log = logging.getLogger(__name__)

BackendName = Literal["cuml", "xgboost-gpu", "sklearn"]
BACKENDS: tuple[BackendName, ...] = ("cuml", "xgboost-gpu", "sklearn")

# skops type name for the tree head. model_store allowlists this exact string.
# The class only walks numeric arrays; it does not call pickle or arbitrary code.
TREE_SKOPS_TYPE = "src.accel.TreeEnsembleClassifier"

# liblinear and cuML's quasi-Newton solver do not share a solution path.
# Same features, weights and seed: mean |ΔP(real)| at most this, and the
# escalated sets must still overlap this much. See tests/test_accel.py.
LOGISTIC_PROB_ATOL = 0.05
LOGISTIC_JACCARD_MIN = 0.90
# Converted CPU model against the model that produced the coefficients or
# the trees (float32 leaf values). Nearly exact; not a solver tolerance.
CONVERT_PROB_ATOL = 1e-4

NODE_DTYPE = np.dtype(
    [
        ("feature", np.int16),  # -1 on a leaf
        ("threshold", np.float32),
        ("yes", np.int32),
        ("no", np.int32),
        ("missing", np.int32),
        ("leaf", np.float64),
    ]
)

_cuda_cache: bool | None = None
_backend_choice: dict[str, BackendName] = {}
_gpu_mem_cache: tuple[float, float | None] | None = None
_GPU_MEM_TTL_S = 15.0


class AccelUnavailable(RuntimeError):
    """The requested GPU backend cannot be used on this machine."""


def clear_backend_cache() -> None:
    """Forget the CUDA probe. Tests use this after patching the probe."""
    global _cuda_cache, _gpu_mem_cache
    _cuda_cache = None
    _gpu_mem_cache = None
    _backend_choice.clear()


def parse_cuda_version(nvidia_smi_text: str) -> str | None:
    """Pull ``CUDA Version: 12.4`` out of nvidia-smi's header. None if absent."""
    for line in nvidia_smi_text.splitlines():
        marker = "CUDA Version:"
        if marker in line:
            tail = line.split(marker, 1)[1].strip()
            version = tail.split()[0] if tail else ""
            return version or None
    return None


def _nvidia_smi_text() -> str | None:
    try:
        out = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _nvidia_device_node() -> bool:
    return Path("/dev/nvidia0").exists() or Path("/proc/driver/nvidia/version").exists()


def cuda_available() -> bool:
    """True when a CUDA device is visible. A CUDA-enabled wheel is not enough.

    XGBoost's ``device='cuda'`` silently switches to CPU when no device is
    visible, so this probe looks for the driver (nvidia-smi or the device
    node) and never treats a successful import as a GPU.
    """
    global _cuda_cache
    if _cuda_cache is None:
        text = _nvidia_smi_text()
        _cuda_cache = bool(text) or _nvidia_device_node()
    return _cuda_cache


def cuda_version() -> str | None:
    """Driver-reported CUDA version, or None when nvidia-smi does not answer."""
    text = _nvidia_smi_text()
    if not text:
        return None
    return parse_cuda_version(text)


def cuml_usable() -> bool:
    """cuML imports and a CUDA device is visible."""
    if not cuda_available():
        return False
    try:
        import cuml.linear_model  # noqa: F401
    except Exception:
        log.info("cuML is not importable; logistic GPU fit is unavailable")
        return False
    return True


def xgboost_gpu_usable() -> bool:
    """XGBoost imports, was built with CUDA, and a CUDA device is visible."""
    if not cuda_available():
        return False
    try:
        import xgboost as xgb
    except Exception:
        log.info("xgboost is not importable; tree GPU fit is unavailable")
        return False
    info: dict[str, Any] = {}
    if hasattr(xgb, "build_info"):
        try:
            info = dict(xgb.build_info())
        except Exception:
            log.info("xgboost build_info failed", exc_info=True)
            return False
    return bool(info.get("USE_CUDA"))


def available_backends() -> dict[str, bool]:
    return {
        "sklearn": True,
        "cuml": cuml_usable(),
        "xgboost-gpu": xgboost_gpu_usable(),
    }


def active_backend() -> BackendName:
    """The backend ``get_backend`` would pick, cached for this process.

    ``/health`` calls this on a timer. The choice is logged once per requested
    name, not on every probe. ``clear_backend_cache`` forgets it.
    """
    requested = (os.environ.get("ACCEL_BACKEND") or "auto").strip().lower() or "auto"
    chosen = _backend_choice.get(requested)
    if chosen is None:
        chosen = get_backend(requested)
        _backend_choice[requested] = chosen
    return chosen


def parse_gpu_memory_mib(text: str) -> float | None:
    """Sum ``memory.used`` lines from ``nvidia-smi --query-gpu=memory.used``.

    Non-numeric lines (a GPU that reports ``[N/A]``) are skipped. None when
    no device reported a number.
    """
    total = 0.0
    found = False
    for line in text.splitlines():
        token = line.strip().split()[0].replace(",", "") if line.strip() else ""
        if not token:
            continue
        try:
            total += float(token)
        except ValueError:
            continue
        found = True
    return total if found else None


def gpu_memory_mib() -> float | None:
    """Used GPU memory in MiB, or None when nvidia-smi does not answer.

    The result is cached for a few seconds so a scrape does not spawn the
    tool on every request. A miss is cached too.
    """
    global _gpu_mem_cache
    now = time.monotonic()
    if _gpu_mem_cache is not None and now - _gpu_mem_cache[0] < _GPU_MEM_TTL_S:
        return _gpu_mem_cache[1]
    value = _query_gpu_memory_mib()
    _gpu_mem_cache = (now, value)
    return value


def _query_gpu_memory_mib() -> float | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return parse_gpu_memory_mib(out.stdout or "")


def get_backend(prefer: str | None = None) -> BackendName:
    """Choose the training backend and log the choice.

    ``prefer`` overrides the ``ACCEL_BACKEND`` environment variable.
    ``auto`` (the default) is always ``sklearn``. cuML is not bit-stable
    across same-seed fits, so it must not replace the published queue just
    because the library imports. Set ``ACCEL_BACKEND=cuml`` or
    ``backend="cuml"`` to use it. ``xgboost-gpu`` is also opt-in: a tree
    booster is a different classifier. Any request that cannot be honoured
    falls back to sklearn.
    """
    requested = prefer if prefer is not None else os.environ.get("ACCEL_BACKEND", "auto")
    requested = (requested or "auto").strip().lower()
    if requested in ("", "auto"):
        chosen: BackendName = "sklearn"
        reason = "auto: sklearn logistic; GPU backends are opt-in"
    elif requested == "sklearn":
        chosen = "sklearn"
        reason = "requested sklearn"
    elif requested == "cuml":
        if cuml_usable():
            chosen = "cuml"
            reason = "requested cuml"
        else:
            chosen = "sklearn"
            reason = "cuml requested but unavailable; sklearn fallback"
    elif requested in ("xgboost-gpu", "xgboost"):
        if xgboost_gpu_usable():
            chosen = "xgboost-gpu"
            reason = "requested xgboost-gpu"
        else:
            chosen = "sklearn"
            reason = "xgboost-gpu requested but unavailable; sklearn fallback"
    else:
        chosen = "sklearn"
        reason = f"unknown backend {requested!r}; sklearn fallback"
    usable = available_backends()
    if usable["xgboost-gpu"] and chosen != "xgboost-gpu":
        reason += "; xgboost-gpu is available and not selected"
    log.info("accel backend %s (%s)", chosen, reason)
    return chosen


def _as_dense(x: Any) -> np.ndarray:
    if hasattr(x, "toarray"):
        x = x.toarray()
    return np.asarray(x, dtype=np.float64)


def _to_numpy(value: Any) -> np.ndarray:
    if hasattr(value, "get"):
        value = value.get()
    return np.asarray(value)


def install_linear_head(
    clf: LogisticRegression,
    coef: np.ndarray,
    intercept: np.ndarray,
    classes: np.ndarray,
) -> LogisticRegression:
    """Install coefficients on a sklearn logistic head. No second solve.

    The head must be the ``LogisticRegression`` already sitting in the alert
    pipeline. Prediction then uses sklearn, which is what skops persists.
    """
    coef = _to_numpy(coef).astype(np.float64, copy=False)
    if coef.ndim == 1:
        coef = coef.reshape(1, -1)
    elif coef.ndim == 2 and coef.shape[0] == 2:
        # A two-row cuML coefficient is class 0 then class 1. sklearn's binary
        # head stores a single row for classes_[1].
        coef = coef[1:] - coef[:1]
    intercept_arr = _to_numpy(intercept).astype(np.float64, copy=False).reshape(-1)
    if intercept_arr.shape[0] == 2:
        intercept_arr = intercept_arr[1:] - intercept_arr[:1]
    if coef.ndim != 2 or coef.shape[0] != 1 or intercept_arr.shape != (1,):
        raise AccelUnavailable(
            f"cannot convert coefficients with shapes {coef.shape} and {intercept_arr.shape}"
        )
    if not np.isfinite(coef).all() or not np.isfinite(intercept_arr).all():
        raise AccelUnavailable("non-finite logistic coefficients")
    clf.classes_ = np.asarray(classes)
    clf.coef_ = coef
    clf.intercept_ = intercept_arr
    clf.n_features_in_ = int(coef.shape[1])
    clf.n_iter_ = np.array([0], dtype=np.int32)
    return clf


def cuml_logistic_coefficients(
    x: Any,
    y: np.ndarray,
    sample_weight: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Fit cuML logistic regression and return numpy coefficients.

    The feature matrix is the already-fitted sklearn transformer output.
    cuML is not imported when this function is not called.
    """
    import cuml.linear_model

    dense = _as_dense(x).astype(np.float32, copy=False)
    model = cuml.linear_model.LogisticRegression(
        penalty="l2",
        C=1.0,
        max_iter=500,
        fit_intercept=True,
        output_type="numpy",
    )
    model.fit(dense, np.asarray(y), sample_weight=np.asarray(sample_weight, dtype=np.float32))
    return _to_numpy(model.coef_), _to_numpy(model.intercept_)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.asarray(z, dtype=np.float64)
    out = np.empty_like(z)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    out[~positive] = exp_z / (1.0 + exp_z)
    return out


def base_margin_from_xgb(raw: Any) -> float:
    """XGBoost 2/3 stores ``base_score`` as a probability. Return the log-odds.

    The config value is often the string ``[9.09E-1]``. A number outside
    (0, 1) is already a margin and is returned as-is.
    """
    if isinstance(raw, str):
        text = raw.strip()
        parsed: Any = json.loads(text) if text.startswith("[") else float(text)
        raw = parsed
    if isinstance(raw, (list, tuple)):
        raw = raw[0]
    value = float(raw)
    if 0.0 < value < 1.0:
        value = float(np.clip(value, 1e-6, 1.0 - 1e-6))
        return float(np.log(value / (1.0 - value)))
    return value


def _parse_one_tree(payload: dict) -> np.ndarray:
    flat: dict[int, dict] = {}

    def walk(node: dict) -> None:
        flat[int(node["nodeid"])] = node
        for child in node.get("children") or []:
            walk(child)

    walk(payload)
    if not flat:
        raise AccelUnavailable("XGBoost dump contained an empty tree")
    arr = np.zeros(max(flat) + 1, dtype=NODE_DTYPE)
    arr["feature"] = np.int16(-1)
    arr["yes"] = np.int32(-1)
    arr["no"] = np.int32(-1)
    arr["missing"] = np.int32(-1)
    for index, node in flat.items():
        if "leaf" in node:
            arr[index]["leaf"] = float(node["leaf"])
            continue
        feature_name = str(node["split"])
        if not feature_name.startswith("f") or not feature_name[1:].isdigit():
            raise AccelUnavailable(f"unexpected XGBoost split {feature_name!r}")
        arr[index]["feature"] = np.int16(int(feature_name[1:]))
        # float32 matches the comparison XGBoost's hist builder uses.
        arr[index]["threshold"] = np.float32(node["split_condition"])
        arr[index]["yes"] = np.int32(node["yes"])
        arr[index]["no"] = np.int32(node["no"])
        arr[index]["missing"] = np.int32(node["missing"])
    return arr


def trees_from_xgb_dump(dumps: list[str], base_score: Any) -> tuple[list[np.ndarray], float]:
    """Parse ``Booster.get_dump(dump_format='json')`` into walker trees."""
    if not dumps:
        raise AccelUnavailable("XGBoost booster has no trees")
    trees = [_parse_one_tree(json.loads(dump)) for dump in dumps]
    return trees, base_margin_from_xgb(base_score)


def ensemble_from_booster(booster: Any) -> TreeEnsembleClassifier:
    """Convert a fitted XGBoost booster to the skops-persistable tree head."""
    dumps = list(booster.get_dump(dump_format="json"))
    config = json.loads(booster.save_config())
    raw_base = config["learner"]["learner_model_param"]["base_score"]
    trees, base = trees_from_xgb_dump(dumps, raw_base)
    return TreeEnsembleClassifier(trees=trees, base_score=base)


class TreeEnsembleClassifier(BaseEstimator, ClassifierMixin):
    """Sum of binary trees, then a sigmoid. The CPU form of an XGBoost booster.

    ``trees`` is a list of structured arrays (one per tree) using
    ``NODE_DTYPE``. ``base_score`` is a log-odds offset, not a probability.
    skops can store this estimator; it cannot store ``xgboost.Booster``.
    """

    def __init__(self, trees: list[np.ndarray] | None = None, base_score: float = 0.0) -> None:
        self.trees = trees
        self.base_score = base_score

    def fit(self, x: Any, y: np.ndarray, sample_weight: np.ndarray | None = None) -> TreeEnsembleClassifier:
        del sample_weight
        dense = _as_dense(x)
        self.classes_ = np.unique(np.asarray(y))
        self.n_features_in_ = int(dense.shape[1])
        if not self.trees:
            raise AccelUnavailable("tree ensemble has no trees to fit")
        return self

    def predict_proba(self, x: Any) -> np.ndarray:
        if not hasattr(self, "classes_"):
            raise RuntimeError("TreeEnsembleClassifier has not been fit")
        dense = np.ascontiguousarray(_as_dense(x), dtype=np.float32)
        if dense.ndim != 2:
            raise ValueError("tree ensemble expects a 2-d feature matrix")
        margin = np.full(dense.shape[0], float(self.base_score), dtype=np.float64)
        for tree in self.trees or []:
            margin += _tree_leaves(tree, dense)
        positive = _sigmoid(margin)
        return np.column_stack([1.0 - positive, positive])


def _tree_leaves(tree: np.ndarray, x: np.ndarray) -> np.ndarray:
    if tree.dtype != NODE_DTYPE:
        tree = np.asarray(tree, dtype=NODE_DTYPE)
    n_nodes = int(tree.shape[0])
    features = tree["feature"]
    if np.any(features >= x.shape[1]):
        raise ValueError("tree split feature is outside the feature matrix")
    node_ids = np.zeros(x.shape[0], dtype=np.int32)
    for _ in range(n_nodes):
        nodes = tree[node_ids]
        is_leaf = nodes["feature"] < 0
        if bool(np.all(is_leaf)):
            break
        safe_feature = np.clip(nodes["feature"], 0, x.shape[1] - 1)
        values = x[np.arange(x.shape[0]), safe_feature]
        missing = np.isnan(values)
        go_yes = (~missing) & (values < nodes["threshold"])
        nxt = np.where(missing, nodes["missing"], np.where(go_yes, nodes["yes"], nodes["no"]))
        bad = (~is_leaf) & ((nxt < 0) | (nxt >= n_nodes))
        if np.any(bad):
            raise ValueError("tree node points outside the tree")
        node_ids = np.where(is_leaf, node_ids, nxt).astype(np.int32, copy=False)
    else:
        raise ValueError("tree walk did not reach a leaf")
    return tree["leaf"][node_ids]


def xgboost_gpu_params(seed: int) -> dict[str, Any]:
    """Hyperparameters for the explicit tree backend. ``device`` is CUDA.

    Kept in one place so a test can fit the same booster and compare it to
    the converted head.
    """
    return {
        "n_estimators": 40,
        "max_depth": 3,
        "learning_rate": 0.1,
        "objective": "binary:logistic",
        "tree_method": "hist",
        "device": "cuda",
        "random_state": seed,
        "n_jobs": 1,
        "verbosity": 0,
    }


def is_cuda_device(device: object) -> bool:
    """True for ``cuda`` and ``cuda:N`` (XGBoost 3.4 reports ``cuda:0``).

    Anything else, including a silent CPU fallback, is not a GPU fit.
    """
    if not isinstance(device, str):
        return False
    text = device.strip().lower()
    if text == "cuda":
        return True
    index = text.removeprefix("cuda:")
    return text.startswith("cuda:") and index.isdigit() and index != ""


def xgboost_gpu_ensemble(
    x: Any,
    y: np.ndarray,
    sample_weight: np.ndarray,
    seed: int,
) -> TreeEnsembleClassifier:
    """Fit an XGBoost booster on CUDA and convert it to ``TreeEnsembleClassifier``.

    ``fail_on_invalid_gpu_id`` is not reliable on the sklearn wrapper (3.x
    still falls back to CPU). After fit, a booster whose saved device is not
    ``cuda`` or ``cuda:N`` is refused so a silent CPU fallback is not labelled
    GPU. XGBoost 3.4 writes ``cuda:0``.
    """
    import xgboost as xgb

    dense = _as_dense(x)
    model = xgb.XGBClassifier(**xgboost_gpu_params(seed))
    model.fit(dense, np.asarray(y), sample_weight=np.asarray(sample_weight, dtype=np.float64))
    config = json.loads(model.get_booster().save_config())
    device = config["learner"]["generic_param"].get("device")
    if not is_cuda_device(device):
        raise AccelUnavailable(f"xgboost device is {device!r}, not cuda")
    head = ensemble_from_booster(model.get_booster())
    head.fit(dense, y)
    return head
