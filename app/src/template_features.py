"""Optional log-template features. Off unless the caller asks for them.

The default encoder is a 4-number hash of the normalised template plus a count
of earlier copies of that same template. Both are known at the alert's own
timestamp. A sentence-transformer file is used only when ``LLM_TEMPLATE_ENCODER``
points at a file whose SHA-256 is ``LLM_TEMPLATE_ENCODER_SHA256``. A missing
file, a hash mismatch, or a missing library skips that encoder and the hash
is used instead. Nothing here reads a realised fault label.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
import pandas as pd

from .entities import Alert
from .features import normalize_message

log = logging.getLogger(__name__)

DIM = 4
TEMPLATE_COLUMNS = ["tpl_h0", "tpl_h1", "tpl_h2", "tpl_h3", "tpl_prior"]
HASH_ENCODER = "hash-v1"
_ON = frozenset({"1", "true", "yes", "on"})


class TemplateEncoder(Protocol):
    name: str

    def embed(self, template: str) -> tuple[float, ...]: ...


class HashTemplateEncoder:
    """Deterministic encoder. No weights and no network."""

    name = HASH_ENCODER

    def embed(self, template: str) -> tuple[float, float, float, float]:
        digest = hashlib.sha256(template.encode("utf-8")).digest()
        values: list[float] = []
        for index in range(DIM):
            word = int.from_bytes(digest[index * 4 : (index + 1) * 4], "big")
            values.append(word / float(2**31) - 1.0)
        return (values[0], values[1], values[2], values[3])


class _ProjectedEncoder:
    """First-load projection of a sentence embedding down to four numbers.

    The projection is a fixed seed, so the same text always yields the same
    four numbers. It is a function of that text alone.
    """

    def __init__(self, model: Any, name: str) -> None:
        self.name = name
        self._model = model
        self._proj: np.ndarray | None = None

    def embed(self, template: str) -> tuple[float, float, float, float]:
        raw = np.asarray(self._model.encode([template]), dtype=float).reshape(-1)
        if self._proj is None or self._proj.shape[1] != raw.shape[0]:
            self._proj = np.random.default_rng(0).normal(size=(DIM, raw.shape[0]))
        reduced = np.tanh(self._proj @ raw)
        return (float(reduced[0]), float(reduced[1]), float(reduced[2]), float(reduced[3]))


def template_features_enabled() -> bool:
    """Production queue reads this. Unset means the current feature columns."""
    return os.environ.get("LLM_TEMPLATE_FEATURES", "").strip().lower() in _ON


def prior_counts(templates: list[str], timestamps: list[pd.Timestamp]) -> list[int]:
    """Copies of the same template with an earlier timestamp.

    Same-timestamp rows are ordered by their original position, so a row sees
    another row at the same instant only when that row is earlier in the batch.
    A later timestamp is never counted.
    """
    seen: dict[str, int] = {}
    priors = [0] * len(templates)
    order = sorted(range(len(templates)), key=lambda i: (timestamps[i], i))
    for index in order:
        key = templates[index]
        priors[index] = seen.get(key, 0)
        seen[key] = priors[index] + 1
    return priors


def template_rows(alerts: list[Alert], encoder: object | None = None) -> list[dict[str, float]]:
    """One dict of TEMPLATE_COLUMNS per alert, using only rows at or before it."""
    chosen = resolve_encoder(encoder)
    templates = [normalize_message(alert.message) for alert in alerts]
    priors = prior_counts(templates, [alert.timestamp for alert in alerts])
    rows: list[dict[str, float]] = []
    for template, prior in zip(templates, priors):
        embedded = _embed(chosen, template)
        row = {f"tpl_h{index}": embedded[index] for index in range(DIM)}
        row["tpl_prior"] = float(prior)
        rows.append(row)
    return rows


def resolve_encoder(encoder: object | None = None) -> TemplateEncoder:
    """The caller-supplied encoder, a verified sentence encoder, or the hash."""
    if encoder is not None:
        return cast(TemplateEncoder, encoder)
    pinned = _try_pinned_sentence_encoder()
    if pinned is not None:
        return pinned
    return HashTemplateEncoder()


def _embed(encoder: TemplateEncoder, template: str) -> tuple[float, float, float, float]:
    raw = tuple(float(value) for value in encoder.embed(template))
    if len(raw) != DIM:
        raise ValueError(f"template encoder must return {DIM} numbers")
    return (raw[0], raw[1], raw[2], raw[3])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _try_pinned_sentence_encoder() -> TemplateEncoder | None:
    path = os.environ.get("LLM_TEMPLATE_ENCODER", "").strip()
    expected = os.environ.get("LLM_TEMPLATE_ENCODER_SHA256", "").strip().lower()
    if not path or len(expected) != 64:
        return None
    file = Path(path)
    if not file.is_file():
        log.info("template encoder weights missing (%s); using %s", file.name, HASH_ENCODER)
        return None
    try:
        digest = _sha256(file)
    except OSError:
        log.error("template encoder weights unreadable; using %s", HASH_ENCODER)
        return None
    if digest != expected:
        log.error("template encoder hash mismatch for %s; using %s", file.name, HASH_ENCODER)
        return None
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        log.info("sentence-transformers is not installed; using %s", HASH_ENCODER)
        return None
    try:
        model = SentenceTransformer(str(file))
    except Exception:
        log.warning("template encoder failed to load; using %s", HASH_ENCODER, exc_info=True)
        return None
    return _ProjectedEncoder(model, f"sentence-transformers@{digest[:12]}")
