"""Keep the last trained model so small live batches can be scored without retraining."""
from __future__ import annotations

import hashlib
import logging
import os
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
