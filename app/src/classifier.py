"""Cost-sensitive alert classifier (design spec section 4.3).

A missed fault weighs `miss_cost` times a false alarm. At decision time the
daily budget is spent on the highest P(real) alerts; exact duplicates are
suppressed first so they do not consume a review slot.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

from .config import Config
from .entities import Action, Alert, Decision
from .features import NUMERIC_COLUMNS, build_feature_frame


class AlertClassifier:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self._pipe: Pipeline | None = None

    def fit(self, alerts: list[Alert], spikes: list | None = None) -> "AlertClassifier":
        frame = build_feature_frame(alerts, self.config, spikes)
        labels = frame["label"].to_numpy()
        if len(np.unique(labels)) < 2:
            raise ValueError("Training data needs both fault and noise alerts")
        weights = np.where(labels == 1, self.config.miss_cost, self.config.false_alarm_cost)
        self._pipe = _make_pipeline(self.config.random_seed)
        self._pipe.fit(frame, labels, clf__sample_weight=weights)
        return self

    def predict_proba(self, alerts: list[Alert], spikes: list | None = None) -> np.ndarray:
        """Score alerts. Pass the jumps of the SAME period as `alerts`; with
        `spikes=None` the jump feature is 0 (training jumps are never reused)."""
        if self._pipe is None:
            raise RuntimeError("Classifier has not been fit")
        frame = build_feature_frame(alerts, self.config, spikes)
        return self._pipe.predict_proba(frame)[:, 1]


def classifier_decisions(
    alerts: list[Alert], proba: np.ndarray, config: Config
) -> list[Decision]:
    """Escalate the top-K non-duplicate alerts per day, ranked by P(real)."""
    decisions: list[Decision | None] = [None] * len(alerts)
    seen: set[tuple[str, str, str]] = set()
    candidates: dict[pd.Timestamp, list[tuple[float, int]]] = defaultdict(list)

    for idx, alert in enumerate(alerts):
        key = (alert.source, alert.alert_type, alert.message)
        score = float(proba[idx])
        if key in seen:
            decisions[idx] = Decision(
                alert.alert_id, Action.SUPPRESS, score, "exact duplicate"
            )
            continue
        seen.add(key)
        day = alert.timestamp.normalize()
        candidates[day].append((score, idx))

    chosen: set[int] = set()
    for day_candidates in candidates.values():
        day_candidates.sort(key=lambda item: -item[0])
        for _, idx in day_candidates[: config.budget_k]:
            chosen.add(idx)

    for idx, alert in enumerate(alerts):
        if decisions[idx] is not None:
            continue
        score = float(proba[idx])
        if idx in chosen:
            decisions[idx] = Decision(
                alert.alert_id, Action.ESCALATE, score, "classifier top-k"
            )
        else:
            decisions[idx] = Decision(
                alert.alert_id, Action.SUPPRESS, score, "below daily budget"
            )
    return decisions  # type: ignore[return-value]


def _make_pipeline(seed: int) -> Pipeline:
    features = ColumnTransformer(
        [
            ("num", StandardScaler(), NUMERIC_COLUMNS),
            (
                "cat",
                OneHotEncoder(handle_unknown="ignore"),
                ["source", "severity"],
            ),
            (
                "txt",
                Pipeline(
                    [
                        ("ravel", FunctionTransformer(np.ravel, feature_names_out="one-to-one")),
                        ("tfidf", TfidfVectorizer(max_features=300, min_df=2, ngram_range=(1, 2))),
                    ]
                ),
                ["message_norm"],
            ),
        ]
    )
    return Pipeline(
        [
            ("features", features),
            (
                "clf",
                LogisticRegression(max_iter=500, solver="liblinear", random_state=seed),
            ),
        ]
    )
