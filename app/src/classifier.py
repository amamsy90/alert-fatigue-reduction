"""Cost-sensitive alert classifier (design spec section 4.3).

A missed fault weighs `miss_cost` times a false alarm. At decision time the
daily budget is spent on the highest P(real) alerts; exact duplicates are
suppressed first so they do not consume a review slot.

Training may use an optional GPU backend (`src.accel`). The pipeline stored
on this object is always scikit-learn, so `load_model` does not need the GPU
libraries. If the requested backend cannot run, fit falls back to sklearn.
"""
from __future__ import annotations

import logging
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

from .accel import (
    AccelUnavailable,
    cuml_logistic_coefficients,
    get_backend,
    install_linear_head,
    xgboost_gpu_ensemble,
)
from .config import Config, day_of
from .entities import Action, Alert, Decision
from .features import NUMERIC_COLUMNS, build_feature_frame
from .template_features import encoder_width, template_columns, template_features_enabled

log = logging.getLogger(__name__)


class AlertClassifier:
    def __init__(
        self,
        config: Config | None = None,
        *,
        backend: str | None = None,
        template_features: bool | None = None,
        encoder: object | None = None,
    ) -> None:
        self.config = config or Config()
        self.backend = backend
        self.backend_used = "sklearn"
        self._pipe: Pipeline | None = None
        # None follows LLM_TEMPLATE_FEATURES. The default is off, so a normal
        # fit still uses NUMERIC_COLUMNS only (FR-22).
        self.template_features = (
            template_features_enabled() if template_features is None else bool(template_features)
        )
        self._template_encoder = encoder
        self.template_encoder_name: str | None = getattr(encoder, "name", None)

    def fit(self, alerts: list[Alert], spikes: list | None = None) -> AlertClassifier:
        frame = self._frame(alerts, spikes)
        labels = frame["label"].to_numpy()
        if len(np.unique(labels)) < 2:
            raise ValueError("Training data needs both fault and noise alerts")
        weights = np.where(labels == 1, self.config.miss_cost, self.config.false_alarm_cost)
        chosen = get_backend(self.backend)
        numeric = self._numeric_columns()
        # The sklearn path is the one the published queue uses. Keep it a
        # single pipeline.fit so a CPU install is unchanged.
        if chosen == "sklearn":
            self._pipe = _fit_sklearn(frame, labels, weights, self.config.random_seed, numeric)
            self.backend_used = "sklearn"
            return self
        try:
            self._pipe = _fit_gpu(chosen, frame, labels, weights, self.config.random_seed, numeric)
            self.backend_used = chosen
        except Exception:
            # FR-21: a GPU failure must still produce the sklearn queue.
            log.warning("accel: %s fit failed; using sklearn", chosen, exc_info=True)
            self._pipe = _fit_sklearn(frame, labels, weights, self.config.random_seed, numeric)
            self.backend_used = "sklearn"
        return self

    def predict_proba(self, alerts: list[Alert], spikes: list | None = None) -> np.ndarray:
        """Score alerts. Pass the jumps of the SAME period as `alerts`; with
        `spikes=None` the jump feature is 0 (training jumps are never reused)."""
        if self._pipe is None:
            raise RuntimeError("Classifier has not been fit")
        frame = self._frame(alerts, spikes)
        return self._pipe.predict_proba(frame)[:, 1]

    def _numeric_columns(self) -> list[str] | None:
        if not self.template_features:
            return None
        encoder = self._active_encoder()
        width = encoder_width(encoder) if encoder is not None else 4
        return list(NUMERIC_COLUMNS) + template_columns(width)

    def _frame(self, alerts: list[Alert], spikes: list | None) -> pd.DataFrame:
        return build_feature_frame(
            alerts,
            self.config,
            spikes,
            template_features=self.template_features,
            encoder=self._active_encoder(),
        )

    def _active_encoder(self) -> object | None:
        """Encoder for the template columns.

        A caller-supplied encoder is kept. A saved ``hash-v1`` model stays on
        the hash. Otherwise (a new classifier, or a saved sentence-encoder
        name) ``resolve_encoder`` reads ``LLM_TEMPLATE_ENCODER``.
        """
        if not self.template_features:
            return None
        if self._template_encoder is None:
            from .template_features import HASH_ENCODER, HashTemplateEncoder, resolve_encoder

            if self.template_encoder_name == HASH_ENCODER:
                self._template_encoder = HashTemplateEncoder()
            else:
                self._template_encoder = resolve_encoder(None)
            self.template_encoder_name = str(getattr(self._template_encoder, "name", HASH_ENCODER))
        return self._template_encoder


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
        day = day_of(alert.timestamp, config)
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


def _fit_sklearn(
    frame: pd.DataFrame,
    labels: np.ndarray,
    weights: np.ndarray,
    seed: int,
    numeric: list[str] | None = None,
) -> Pipeline:
    pipe = _make_pipeline(seed, numeric)
    pipe.fit(frame, labels, clf__sample_weight=weights)
    return pipe


def _fit_gpu(
    backend: str,
    frame: pd.DataFrame,
    labels: np.ndarray,
    weights: np.ndarray,
    seed: int,
    numeric: list[str] | None = None,
) -> Pipeline:
    """Train the head on the GPU, then store a sklearn pipeline.

    The text and category transformer stays on CPU. Only the linear or tree
    head is trained on the device. cuML coefficients are copied onto
    ``LogisticRegression``. An XGBoost booster is replaced by
    ``TreeEnsembleClassifier`` (skops cannot persist the booster).
    """
    pipe = _make_pipeline(seed, numeric)
    features = pipe.named_steps["features"]
    features.fit(frame)
    transformed = features.transform(frame)
    if backend == "cuml":
        coef, intercept = cuml_logistic_coefficients(transformed, labels, weights)
        install_linear_head(pipe.named_steps["clf"], coef, intercept, np.unique(labels))
        return pipe
    if backend == "xgboost-gpu":
        head = xgboost_gpu_ensemble(transformed, labels, weights, seed)
        pipe.steps[-1] = ("clf", head)
        return pipe
    raise AccelUnavailable(backend)


def _make_pipeline(seed: int, numeric: list[str] | None = None) -> Pipeline:
    # None keeps the published numeric block. Extra columns are opt-in.
    columns = list(NUMERIC_COLUMNS if numeric is None else numeric)
    features = ColumnTransformer(
        [
            ("num", StandardScaler(), columns),
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
