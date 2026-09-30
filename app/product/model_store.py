"""Model versions: every trained model is kept with a model card; one is active.

The active file is still named `model.pkl` so existing paths keep working, but
it is a zip of JSON plus a scikit-learn file written by skops. Nothing in that
file is unpickled. Every version is `models/vNNNN.pkl` with a SHA-256 and an
entry in `models/registry.json` (the model card: when, on what data, with
which code, how it scored on its held-out window, and whether it passed the
promotion gate). `activate_version` rolls back to any earlier version.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import skops.io as sio

from src.classifier import AlertClassifier

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "model.pkl"
RETRAIN_AFTER_DAYS = 7
# Zip of meta.json and, when a classifier was fit, pipe.skops. Not a pickle.
MODEL_FORMAT = "oncall-model-v1"
# numpy.ravel is the FunctionTransformer inside the text branch. Anything else
# beside skops' own sklearn types is refused, so a file cannot name os.system.
TRUSTED_TYPES = frozenset({"numpy.ravel"})


@dataclass
class SavedModel:
    model: AlertClassifier | None
    escalate_threshold: float
    review_threshold: float
    label_source: str
    trained_at: str
    train_alerts: int
    # "Real" marks that existed when this model was trained.
    feedback_real: int

    def status(self, feedback_real_now: int, now: pd.Timestamp | None = None) -> dict:
        now = now or pd.Timestamp.now("UTC")
        age_days = (now - pd.Timestamp(self.trained_at)).total_seconds() / 86400
        new_marks = max(0, feedback_real_now - self.feedback_real)
        reasons = []
        if age_days >= RETRAIN_AFTER_DAYS:
            reasons.append(f"model is {age_days:.0f} days old")
        if new_marks:
            reasons.append(f"{new_marks} new Real marks since training")
        return {
            "trained_at": self.trained_at,
            "label_source": self.label_source,
            "train_alerts": self.train_alerts,
            "age_days": round(age_days, 2),
            "new_real_marks": new_marks,
            "retrain_due": bool(reasons),
            "retrain_reasons": reasons,
        }


def model_path() -> Path:
    return Path(os.environ.get("MODEL_PATH") or DEFAULT_PATH)


log = logging.getLogger(__name__)


def _digest_path(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _meta(saved: SavedModel, has_pipeline: bool) -> dict:
    return {
        "format": MODEL_FORMAT,
        "has_pipeline": has_pipeline,
        "escalate_threshold": saved.escalate_threshold,
        "review_threshold": saved.review_threshold,
        "label_source": saved.label_source,
        "trained_at": saved.trained_at,
        "train_alerts": saved.train_alerts,
        "feedback_real": saved.feedback_real,
    }


def _pipeline_bytes(pipe: object) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".skops", delete=False) as handle:
        skops_path = Path(handle.name)
    try:
        sio.dump(pipe, skops_path)
        return skops_path.read_bytes()
    finally:
        skops_path.unlink(missing_ok=True)


def _load_pipeline(blob: bytes) -> object | None:
    with tempfile.NamedTemporaryFile(suffix=".skops", delete=False) as handle:
        handle.write(blob)
        skops_path = Path(handle.name)
    try:
        unknown = set(sio.get_untrusted_types(file=skops_path))
        extra = unknown - TRUSTED_TYPES
        if extra:
            log.error("Refusing model: unexpected types %s", sorted(extra))
            return None
        return sio.load(skops_path, trusted=sorted(TRUSTED_TYPES & unknown))
    finally:
        skops_path.unlink(missing_ok=True)


def save_model(saved: SavedModel, path: Path | None = None) -> None:
    """Write a zip (JSON plus skops) and a SHA-256 next to it. Never pickle."""
    path = path or model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pipe = None if saved.model is None else getattr(saved.model, "_pipe", None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("meta.json", json.dumps(_meta(saved, pipe is not None)))
        if pipe is not None:
            archive.writestr("pipe.skops", _pipeline_bytes(pipe))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.getvalue())
    digest = _sha256(tmp)
    tmp.replace(path)
    _digest_path(path).write_text(digest + "\n", encoding="ascii")


def load_model(path: Path | None = None) -> SavedModel | None:
    """Load a model this app wrote. Refuses a missing or mismatched SHA-256,
    a pickle, or a skops file whose types are not on the allowlist."""
    path = path or model_path()
    if not path.exists():
        return None
    digest_file = _digest_path(path)
    if not digest_file.exists():
        log.error("Refusing %s: no SHA-256 file beside it", path)
        return None
    expected = digest_file.read_text(encoding="ascii").strip()
    if _sha256(path) != expected:
        log.error("Refusing %s: SHA-256 does not match %s", path, digest_file.name)
        return None
    raw = path.read_bytes()
    if not raw.startswith(b"PK"):
        log.error("Refusing %s: not a model archive", path)
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            meta = json.loads(archive.read("meta.json"))
            if meta.get("format") != MODEL_FORMAT:
                log.error("Refusing %s: unknown model format", path)
                return None
            model = None
            if meta.get("has_pipeline"):
                pipe = _load_pipeline(archive.read("pipe.skops"))
                if pipe is None:
                    return None
                model = AlertClassifier()
                model._pipe = pipe
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        log.error("Refusing %s: model archive could not be read", path)
        return None
    return SavedModel(
        model=model,
        escalate_threshold=float(meta["escalate_threshold"]),
        review_threshold=float(meta["review_threshold"]),
        label_source=str(meta["label_source"]),
        trained_at=str(meta["trained_at"]),
        train_alerts=int(meta["train_alerts"]),
        feedback_real=int(meta["feedback_real"]),
    )


# ---------------------------------------------------------------------------
# Registry


_REGISTRY_LOCK = threading.Lock()


def models_dir() -> Path:
    return model_path().parent / "models"


def _registry_path() -> Path:
    return models_dir() / "registry.json"


def load_registry() -> dict:
    path = _registry_path()
    if not path.exists():
        return {"active": None, "versions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # An empty registry would look like "no active model" and the next
        # train would promote itself. A job that cannot read the registry fails.
        raise ValueError(f"model registry {path.name} is unreadable; refusing to treat it as empty") from exc
    data.setdefault("active", None)
    data.setdefault("versions", [])
    return data


def _write_registry(data: dict) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def code_version() -> str:
    """Git commit of the running code, or the package version when not in a checkout."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             timeout=5, cwd=Path(__file__).resolve().parent)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "1.0.0"


def held_out_cost(report: dict | None, miss_cost: float, false_alarm_cost: float) -> float | None:
    """Cost of a model's decisions on its held-out window (None without one)."""
    if not report:
        return None
    return miss_cost * report.get("fn", 0) + false_alarm_cost * report.get("fp", 0)


def register_model(saved: SavedModel, card: dict, promote: bool) -> dict:
    """Keep `saved` as the next version with its model card; make it active when
    `promote` is true. Returns the registry entry."""
    with _REGISTRY_LOCK:
        registry = load_registry()
        number = 1 + max((v["number"] for v in registry["versions"]), default=0)
        version = f"v{number:04d}"
        path = models_dir() / f"{version}.pkl"
        save_model(saved, path)
        entry = {
            "version": version,
            "number": number,
            "file": path.name,
            "sha256": _sha256(path),
            "trained_at": saved.trained_at,
            "label_source": saved.label_source,
            "train_alerts": saved.train_alerts,
            "escalate_threshold": saved.escalate_threshold,
            "review_threshold": saved.review_threshold,
            "code_version": code_version(),
            "promoted": bool(promote),
            **card,
        }
        registry["versions"].append(entry)
        if promote:
            _make_active(path, entry, registry)
        _write_registry(registry)
        return entry


def _make_active(path: Path, entry: dict, registry: dict) -> None:
    active = model_path()
    active.parent.mkdir(parents=True, exist_ok=True)
    tmp = active.with_suffix(".activating")
    shutil.copyfile(path, tmp)
    tmp.replace(active)
    _digest_path(active).write_text(entry["sha256"] + "\n", encoding="ascii")
    registry["active"] = entry["version"]
    registry.setdefault("history", []).append(
        {"version": entry["version"], "activated_at": pd.Timestamp.now("UTC").isoformat()}
    )


def activate_version(version: str) -> dict:
    """Roll back (or forward) to a kept version. Refuses a file whose hash changed."""
    with _REGISTRY_LOCK:
        registry = load_registry()
        entry = next((v for v in registry["versions"] if v["version"] == version), None)
        if entry is None:
            raise KeyError(f"Unknown model version {version}")
        path = models_dir() / entry["file"]
        if not path.exists() or _sha256(path) != entry["sha256"]:
            raise ValueError(f"Model file for {version} is missing or changed; refusing to activate it")
        _make_active(path, entry, registry)
        _write_registry(registry)
        return entry
