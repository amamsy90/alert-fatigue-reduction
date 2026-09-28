"""Model versions: every trained model is kept with a model card; one is active.

`model.pkl` is the active model (what live batches are scored with). Every
trained model is also written to `models/vNNNN.pkl` with a SHA-256 and an
entry in `models/registry.json` (the model card: when, on what data, with
which code, how it scored on its held-out window, and whether it passed the
promotion gate). `activate_version` rolls back to any earlier version.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import joblib
import pandas as pd

from src.classifier import AlertClassifier

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "model.pkl"
RETRAIN_AFTER_DAYS = 7


@dataclass
class SavedModel:
    model: AlertClassifier
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


def save_model(saved: SavedModel, path: Path | None = None) -> None:
    """Write the model with joblib and a SHA-256 next to it (model.pkl.sha256)."""
    path = path or model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    joblib.dump(saved, tmp)
    digest = _sha256(tmp)
    tmp.replace(path)
    _digest_path(path).write_text(digest + "\n", encoding="ascii")


def load_model(path: Path | None = None) -> SavedModel | None:
    """Load the model this app wrote. Refuses a file whose SHA-256 does not match
    the one saved with it (a replaced or corrupted file is never unpickled).

    Unpickling runs code, so never point MODEL_PATH at a file from elsewhere.
    A model saved before hashes were written is loaded once and given a hash.
    """
    path = path or model_path()
    if not path.exists():
        return None
    digest_file = _digest_path(path)
    if digest_file.exists():
        expected = digest_file.read_text(encoding="ascii").strip()
        if _sha256(path) != expected:
            log.error("Refusing %s: SHA-256 does not match %s", path, digest_file.name)
            return None
    saved = joblib.load(path)
    if not digest_file.exists():
        digest_file.write_text(_sha256(path) + "\n", encoding="ascii")
    return saved


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
    except (OSError, ValueError):
        return {"active": None, "versions": []}
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
