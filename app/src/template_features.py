"""Optional log-template features. Off unless the caller asks for them.

The default encoder is a 4-number hash of the normalised template plus a count
of earlier copies of that same template. Both are known at the alert's own
timestamp. A sentence-transformer model is used only when ``LLM_TEMPLATE_ENCODER``
points at a weight file whose SHA-256 is ``LLM_TEMPLATE_ENCODER_SHA256``. The
file is hashed; the model is loaded from that file's folder (sentence-transformers
needs a directory). ``LOG_ENCODER=contrastive`` loads the optional InfoNCE
encoder instead. That encoder may use more than four numbers
(``LOG_ENCODER_DIM``); the hash and the sentence encoder stay at four, which
caps what they can add. A missing file, a hash mismatch, a missing library, or
a failed load is logged and recorded, and the hash encoder is used instead.
Nothing here reads a realised fault label.
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
_encoder_state: dict[str, str | None] = {"active": HASH_ENCODER, "requested": None, "error": None}
_encoder_resolved = False


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
        self._cache: dict[str, tuple[float, float, float, float]] = {}

    def embed(self, template: str) -> tuple[float, float, float, float]:
        cached = self._cache.get(template)
        if cached is not None:
            return cached
        raw = np.asarray(self._model.encode([template]), dtype=float).reshape(-1)
        if self._proj is None or self._proj.shape[1] != raw.shape[0]:
            self._proj = np.random.default_rng(0).normal(size=(DIM, raw.shape[0]))
        reduced = np.tanh(self._proj @ raw)
        values = (float(reduced[0]), float(reduced[1]), float(reduced[2]), float(reduced[3]))
        self._cache[template] = values
        return values


def encoder_load_status() -> dict[str, str | None]:
    """Last encoder resolution. ``error`` is set when a requested model did not load.

    Before the first resolve, a set ``LLM_TEMPLATE_ENCODER`` is reported as the
    requested path and ``active`` stays unset. That is not a claim that
    ``hash-v1`` is loaded.
    """
    if not _encoder_resolved:
        path = os.environ.get("LLM_TEMPLATE_ENCODER", "").strip() or None
        if path:
            return {"active": None, "requested": path, "error": None}
        return {"active": HASH_ENCODER, "requested": None, "error": None}
    return dict(_encoder_state)


def _record_encoder(active: str, requested: str | None, error: str | None) -> None:
    global _encoder_resolved
    _encoder_resolved = True
    _encoder_state["active"] = active
    _encoder_state["requested"] = requested
    _encoder_state["error"] = error
    if error:
        log.error("template encoder fell back to %s (%s): %s", active, requested or "unset", error)


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


def template_prior_mean(alerts: list[Alert], encoder: object | None = None) -> float | None:
    """Mean of ``tpl_prior`` for this batch, or None when the flag is off.

    The mean is a summary of the causal columns: each alert's count uses only
    rows at or before that alert. The production drift check stores it on the
    job and skips the check entirely when the flag is off.
    """
    if not template_features_enabled() or not alerts:
        return None
    rows = template_rows(alerts, encoder)
    return sum(row["tpl_prior"] for row in rows) / len(rows)


def template_columns(width: int = DIM) -> list[str]:
    """``tpl_h*`` plus ``tpl_prior``. Width 4 is the hash and MiniLM projection."""
    return [f"tpl_h{index}" for index in range(width)] + ["tpl_prior"]


def encoder_width(encoder: object) -> int:
    """Four, unless this is the contrastive encoder and it saved another width."""
    name = str(getattr(encoder, "name", ""))
    if name.startswith("contrastive"):
        return max(1, int(getattr(encoder, "dim", DIM)))
    return DIM


def template_rows(alerts: list[Alert], encoder: object | None = None) -> list[dict[str, float]]:
    """One dict of template columns per alert, using only rows at or before it."""
    chosen = resolve_encoder(encoder)
    width = encoder_width(chosen)
    templates = [normalize_message(alert.message) for alert in alerts]
    priors = prior_counts(templates, [alert.timestamp for alert in alerts])
    embedded_by_template: dict[str, tuple[float, ...]] = {}
    rows: list[dict[str, float]] = []
    for template, prior in zip(templates, priors):
        embedded = embedded_by_template.get(template)
        if embedded is None:
            embedded = _embed(chosen, template, width)
            embedded_by_template[template] = embedded
        row = {f"tpl_h{index}": value for index, value in enumerate(embedded)}
        row["tpl_prior"] = float(prior)
        rows.append(row)
    return rows


def resolve_encoder(encoder: object | None = None) -> TemplateEncoder:
    """The caller-supplied encoder, a verified file, or the hash.

    ``LOG_ENCODER=contrastive`` tries the InfoNCE file and does not also load
    a sentence transformer. Any other value keeps the previous path: a pinned
    sentence encoder, or the hash.
    """
    if encoder is not None:
        return cast(TemplateEncoder, encoder)
    if os.environ.get("LOG_ENCODER", "").strip().lower() == "contrastive":
        loaded = _try_contrastive_encoder()
        if loaded is not None:
            return loaded
        return HashTemplateEncoder()
    pinned = _try_pinned_sentence_encoder()
    if pinned is not None:
        return pinned
    return HashTemplateEncoder()


def _try_contrastive_encoder() -> TemplateEncoder | None:
    from .log_encoder import load_contrastive_encoder

    loaded, error = load_contrastive_encoder()
    if loaded is None:
        requested = os.environ.get("LOG_ENCODER_PATH", "").strip() or "contrastive"
        _record_encoder(HASH_ENCODER, requested, error or "contrastive encoder unavailable")
        return None
    _record_encoder(loaded.name, os.environ.get("LOG_ENCODER_PATH", "").strip() or None, None)
    log.info("template encoder loaded %s", loaded.name)
    return loaded


def _embed(encoder: TemplateEncoder, template: str, width: int = DIM) -> tuple[float, ...]:
    raw = tuple(float(value) for value in encoder.embed(template))
    if len(raw) != width:
        raise ValueError(f"template encoder must return {width} numbers")
    return raw


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _try_pinned_sentence_encoder() -> TemplateEncoder | None:
    path = os.environ.get("LLM_TEMPLATE_ENCODER", "").strip()
    expected = os.environ.get("LLM_TEMPLATE_ENCODER_SHA256", "").strip().lower()
    if not path and not expected:
        _record_encoder(HASH_ENCODER, None, None)
        return None
    if not path or len(expected) != 64:
        _record_encoder(HASH_ENCODER, path or None, "encoder path or sha256 is incomplete")
        return None
    file = Path(path)
    if not file.is_file():
        _record_encoder(HASH_ENCODER, str(file), "weights file missing")
        return None
    try:
        digest = _sha256(file)
    except OSError as exc:
        _record_encoder(HASH_ENCODER, str(file), f"weights unreadable: {exc}")
        return None
    if digest != expected:
        _record_encoder(HASH_ENCODER, str(file), "hash mismatch")
        return None
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        _record_encoder(HASH_ENCODER, str(file), "sentence-transformers is not installed")
        return None
    folder = str(file.parent)
    try:
        model = SentenceTransformer(folder)
    except Exception as exc:
        _record_encoder(HASH_ENCODER, str(file), f"{type(exc).__name__}: {exc}")
        return None
    name = f"sentence-transformers@{digest[:12]}"
    _record_encoder(name, str(file), None)
    log.info("template encoder loaded %s from %s", name, folder)
    return _ProjectedEncoder(model, name)
