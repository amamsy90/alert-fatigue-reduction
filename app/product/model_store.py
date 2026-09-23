"""Keep the last trained model so small live batches can be scored without retraining."""
from __future__ import annotations

import os
import pickle
from dataclasses import dataclass
from pathlib import Path

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


def save_model(saved: SavedModel, path: Path | None = None) -> None:
    path = path or model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    saved.model._spikes = None  # training spikes are not needed to score new alerts
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(saved, fh)
    tmp.replace(path)


def load_model(path: Path | None = None) -> SavedModel | None:
    """Only ever loads the file this app wrote; never point MODEL_PATH at untrusted files."""
    path = path or model_path()
    if not path.exists():
        return None
    with path.open("rb") as fh:
        return pickle.load(fh)
