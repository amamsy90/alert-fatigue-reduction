"""Contrastive log-template encoder. Off unless ``LOG_ENCODER=contrastive``.

Templates that share a training fault window are pulled together. The encoder
sees the template text only. Fault ids are group keys at training time and
are not features. Rows at or after the split are not training rows.

The saved file is safetensors plus JSON with a SHA-256 sidecar. At decision
time the embedding is a numpy forward pass, so scoring does not import torch.
The width may differ from the 4-number hash and MiniLM projection. That wider
vector is used only for this encoder. Hash-v1 and the sentence encoder stay
at four numbers.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .entities import Alert, Fault
from .features import normalize_message
from .weight_io import load_bytes, save_bytes

log = logging.getLogger(__name__)

FORMAT = "oncall-log-encoder-v1"
WEIGHTS_NAME = "encoder.safetensors"
META_NAME = "encoder.json"
N_FEATURES = 64
DEFAULT_DIM = 4


def log_encoder_requested() -> bool:
    return os.environ.get("LOG_ENCODER", "").strip().lower() == "contrastive"


def featurize(template: str, *, salt: str = "") -> np.ndarray:
    """Signed character trigram hash. A function of the text and the salt only."""
    vector = np.zeros(N_FEATURES, dtype=np.float64)
    raw = f" {salt} {template} "
    if len(raw) < 3:
        raw = raw.ljust(3)
    for index in range(len(raw) - 2):
        digest = hashlib.sha256(raw[index : index + 3].encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % N_FEATURES
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = float(np.linalg.norm(vector))
    if norm > 0:
        vector /= norm
    return vector


def _utc_stamp(stamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def training_groups(alerts: list[Alert], faults: list[Fault], train_end) -> list[tuple[str, str]]:
    """``(template, fault id)`` for alerts inside a training fault window.

    Membership is the window, not the service: a caller alert during the fault
    shares the fault's group. A fault whose component equals the alert's source
    wins when windows overlap. An alert at or after ``train_end``, or a fault
    that starts at or after ``train_end``, is left out. The fault id is the
    group key, not an encoder input.
    """
    boundary = _utc_stamp(train_end)
    rows: list[tuple[str, str]] = []
    for alert in alerts:
        stamp = _utc_stamp(alert.timestamp)
        if stamp >= boundary:
            continue
        template = normalize_message(alert.message)
        chosen: str | None = None
        for fault in faults:
            start = _utc_stamp(fault.start)
            if start >= boundary:
                continue
            end = min(_utc_stamp(fault.end), boundary)
            if start <= stamp < end:
                chosen = fault.fault_id
                if fault.component == alert.source:
                    break
        if chosen is not None:
            rows.append((template, chosen))
    return rows


class ContrastiveEncoder:
    """Numpy embedding of a saved MLP. ``dim`` is this encoder's width."""

    def __init__(self, state: dict[str, np.ndarray], *, dim: int, name: str) -> None:
        self.state = state
        self.dim = int(dim)
        self.name = name

    def embed(self, template: str) -> tuple[float, ...]:
        features = featurize(normalize_message(template) if template else "")
        # Callers pass already-normalised templates from template_rows. Normalising
        # twice is stable: normalize_message is idempotent on its own output.
        hidden = np.maximum(0.0, features @ self.state["w1"].T + self.state["b1"])
        output = hidden @ self.state["w2"].T + self.state["b2"]
        output = np.tanh(output)
        return tuple(float(value) for value in output[: self.dim])


def save_encoder(encoder: ContrastiveEncoder, directory: Path, *, extra: dict | None = None) -> str:
    from safetensors.numpy import save_file

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / WEIGHTS_NAME
    state = {key: np.ascontiguousarray(value, dtype=np.float32) for key, value in encoder.state.items()}
    save_file(state, str(path))
    digest = save_bytes(path, path.read_bytes())
    payload = {
        "format": FORMAT,
        "pages": False,
        "objective": "infonce-fault-window",
        "dim": encoder.dim,
        "n_features": N_FEATURES,
        "weights_sha256": digest,
        "state_keys": sorted(state),
    }
    if extra:
        payload.update(extra)
    save_bytes(directory / META_NAME, json.dumps(payload, sort_keys=True).encode("utf-8"))
    return digest


def _read_state(blob: bytes) -> dict[str, np.ndarray]:
    import tempfile

    from safetensors.numpy import load_file

    with tempfile.NamedTemporaryFile(suffix=".safetensors") as handle:
        handle.write(blob)
        handle.flush()
        loaded = load_file(handle.name)
    return {key: np.asarray(value, dtype=np.float32) for key, value in loaded.items()}


def load_contrastive_encoder(path: str | None = None, expected: str | None = None) -> tuple[ContrastiveEncoder | None, str | None]:
    """Load the pinned encoder. ``(None, reason)`` on any refusal."""
    raw = (path or os.environ.get("LOG_ENCODER_PATH", "")).strip()
    pin = (expected if expected is not None else os.environ.get("LOG_ENCODER_SHA256", "")).strip().lower()
    if not raw:
        return None, "contrastive encoder path is unset"
    file = Path(raw)
    if file.is_dir():
        weights = file / WEIGHTS_NAME
        meta_path = file / META_NAME
    else:
        weights = file
        meta_path = file.with_name(META_NAME)
    meta_bytes, meta_status = load_bytes(meta_path)
    if meta_bytes is None:
        return None, meta_status
    try:
        payload = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "bad-format"
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or payload.get("pages") is not False:
        return None, "bad-format"
    blob, status = load_bytes(weights, payload.get("weights_sha256"))
    if blob is None:
        return None, status
    actual = str(payload.get("weights_sha256", ""))
    if pin and pin != actual.lower():
        return None, "hash-mismatch"
    dim = int(payload.get("dim", DEFAULT_DIM))
    env_dim = os.environ.get("LOG_ENCODER_DIM", "").strip()
    if env_dim:
        try:
            asked = int(env_dim)
        except ValueError:
            return None, "LOG_ENCODER_DIM is not an integer"
        if asked != dim:
            return None, "LOG_ENCODER_DIM does not match the saved encoder"
    try:
        state = _read_state(blob)
    except ImportError:
        return None, "safetensors is not installed"
    except Exception:
        log.warning("contrastive encoder weights could not be read", exc_info=True)
        return None, "bad-format"
    for key in ("w1", "b1", "w2", "b2"):
        if key not in state:
            return None, "bad-format"
    name = f"contrastive@{actual[:12]}"
    return ContrastiveEncoder(state, dim=dim, name=name), None
