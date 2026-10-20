"""Optional trace root-cause model. Off unless ``TRACE_MODEL`` is set.

A small message-passing net scores one service per fault window. The saved
archive is safetensors plus JSON, each with a SHA-256 sidecar. The model does
not add a queue item and it does not page. A missing file, a bad hash, a
pickle, or a missing library leaves the queue untouched.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import numpy as np
import pandas as pd

from .entities import Fault
from .trace_graph import CallGraph, build_graph, root_cause_hit
from .weight_io import load_bytes, save_bytes

log = logging.getLogger(__name__)

_ON = frozenset({"1", "true", "yes", "on"})
FORMAT = "oncall-trace-gnn-v1"
WEIGHTS_NAME = "model.safetensors"
META_NAME = "model.json"
MIN_WINDOWS = 4


def trace_model_enabled() -> bool:
    return os.environ.get("TRACE_MODEL", "").strip().lower() in _ON


def trace_model_directory() -> Path | None:
    raw = os.environ.get("TRACE_MODEL_DIR", "").strip()
    return Path(raw) if raw else None


def torch_stack_available() -> bool:
    try:
        import safetensors.numpy  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def _version(dist_name: str) -> str | None:
    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return None


def _utc(stamp: pd.Timestamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


@dataclass
class TraceArchive:
    hidden: int
    backend: str
    state: dict[str, np.ndarray]
    device: str
    device_requested: str
    fell_back: bool
    gnn_fell_back: bool
    versions: dict[str, str | None] = field(default_factory=dict)
    weights_sha256: str = ""

    def rank_graph(self, graph: CallGraph) -> list[tuple[str, float]]:
        """Services and scores for a graph that was already built. Scoring imports torch."""
        from .trace_gnn import resolve_torch_device, score_graph

        if not graph.services:
            return []
        device, _fell = resolve_torch_device(self.device)
        logits = score_graph(graph, self.state, hidden=self.hidden, device=device, backend=self.backend)
        order = np.argsort(-logits)
        return [(graph.services[int(index)], float(logits[int(index)])) for index in order]

    def score_window(self, spans: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[str, float]]:
        """Services and scores for spans that finished before ``end``.

        The label is not read. Scoring imports torch.
        """
        return self.rank_graph(build_graph(spans, start, end))

    def predict_service(self, spans: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> str | None:
        ranked = self.score_window(spans, start, end)
        return ranked[0][0] if ranked else None

    def predict_graph(self, graph: CallGraph) -> str | None:
        ranked = self.rank_graph(graph)
        return ranked[0][0] if ranked else None


def _window_end(fault: Fault, limit: pd.Timestamp | None) -> pd.Timestamp:
    end = _utc(fault.end)
    if limit is not None:
        end = min(end, _utc(limit))
    return end


def training_graphs(
    spans: pd.DataFrame,
    faults: list[Fault],
    train_end: pd.Timestamp,
    graphs: Mapping[str, CallGraph] | None = None,
) -> tuple[list[CallGraph], list[int]]:
    """Windows that close at or before ``train_end``. The label is the node index.

    A fault that starts before the split is clipped at the split, so a span
    after the split cannot enter a training window. ``graphs`` is a window
    already built for that fault (one RCAEval recording); it is not rebuilt.
    """
    boundary = _utc(train_end)
    built: list[CallGraph] = []
    labels: list[int] = []
    for fault in faults:
        if _utc(fault.start) >= boundary:
            continue
        end = _window_end(fault, boundary)
        if end <= _utc(fault.start):
            continue
        if graphs is not None and fault.fault_id in graphs:
            graph = graphs[fault.fault_id]
        else:
            graph = build_graph(spans, fault.start, end)
        if fault.component not in graph.services:
            continue
        built.append(graph)
        labels.append(graph.services.index(fault.component))
    return built, labels


def fit_trace_model(
    spans: pd.DataFrame,
    faults: list[Fault],
    train_end: pd.Timestamp,
    *,
    hidden: int = 8,
    epochs: int = 20,
    seed: int = 42,
    device: str = "cpu",
    backend: str = "auto",
    graphs: Mapping[str, CallGraph] | None = None,
) -> tuple[TraceArchive | None, str]:
    """Train on fault windows that start before ``train_end``."""
    if not torch_stack_available():
        return None, "torch-missing"
    windows, labels = training_graphs(spans, faults, train_end, graphs)
    if len(windows) < MIN_WINDOWS:
        return None, f"too-few-windows:{len(windows)}<{MIN_WINDOWS}"
    from .trace_gnn import resolve_gnn_backend, resolve_torch_device, train_state

    used, fell_back = resolve_torch_device(device)
    gnn_backend, gnn_fell_back = resolve_gnn_backend(backend)
    state = train_state(
        windows,
        labels,
        hidden=hidden,
        epochs=epochs,
        seed=seed,
        device=used,
        backend=gnn_backend,
    )
    archive = TraceArchive(
        hidden=hidden,
        backend=gnn_backend,
        state=state,
        device=used,
        device_requested=device,
        fell_back=fell_back,
        gnn_fell_back=gnn_fell_back,
        versions={"torch": _version("torch"), "safetensors": _version("safetensors")},
    )
    return archive, "trained"


def predict_services(
    archive: TraceArchive,
    spans: pd.DataFrame,
    faults: list[Fault],
    *,
    not_before: pd.Timestamp | None = None,
    graphs: Mapping[str, CallGraph] | None = None,
) -> dict[str, str | None]:
    """Top service per fault. A span at or after the fault's end is ignored.

    A prebuilt graph is scored as it stands. The span table is not read again.
    """
    out: dict[str, str | None] = {}
    floor = _utc(not_before) if not_before is not None else None
    for fault in faults:
        start = _utc(fault.start)
        if floor is not None and start < floor:
            continue
        if graphs is not None and fault.fault_id in graphs:
            out[fault.fault_id] = archive.predict_graph(graphs[fault.fault_id])
        else:
            out[fault.fault_id] = archive.predict_service(spans, start, _utc(fault.end))
    return out


def caught_ids(predicted: dict[str, str | None], faults: list[Fault]) -> set[str]:
    """Faults whose top service is the root-cause service. A caller does not count."""
    return {fault.fault_id for fault in faults if root_cause_hit(predicted.get(fault.fault_id), fault.component)}


def save_trace_model(archive: TraceArchive, directory: Path) -> str:
    """Write safetensors + JSON. Returns the SHA-256 of the weight file."""
    from safetensors.numpy import save_file

    directory.mkdir(parents=True, exist_ok=True)
    weights_path = directory / WEIGHTS_NAME
    state = {key: np.ascontiguousarray(value, dtype=np.float32) for key, value in archive.state.items()}
    save_file(state, str(weights_path))
    weights_sha = save_bytes(weights_path, weights_path.read_bytes())
    payload = {
        "format": FORMAT,
        "pages": False,
        "objective": "root-cause-service",
        "hidden": archive.hidden,
        "backend": archive.backend,
        "device": archive.device,
        "device_requested": archive.device_requested,
        "fell_back": archive.fell_back,
        "gnn_fell_back": archive.gnn_fell_back,
        "versions": archive.versions,
        "weights_sha256": weights_sha,
        "state_keys": sorted(state),
    }
    save_bytes(directory / META_NAME, json.dumps(payload, sort_keys=True).encode("utf-8"))
    archive.weights_sha256 = weights_sha
    return weights_sha


def _read_state(blob: bytes, load_file) -> dict[str, np.ndarray]:
    with tempfile.NamedTemporaryFile(suffix=".safetensors") as handle:
        handle.write(blob)
        handle.flush()
        loaded = load_file(handle.name)
    return {key: np.asarray(value) for key, value in loaded.items()}


def load_trace_model(directory: Path, expected_sha: str | None = None) -> tuple[TraceArchive | None, str]:
    """Load a hash-pinned archive. Does not import torch (scoring does)."""
    meta_bytes, status = load_bytes(directory / META_NAME)
    if meta_bytes is None:
        return None, status
    try:
        payload = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "bad-format"
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or payload.get("pages") is not False:
        return None, "bad-format"
    pinned = expected_sha or os.environ.get("TRACE_MODEL_SHA256", "")
    weight_bytes, weight_status = load_bytes(directory / WEIGHTS_NAME, payload.get("weights_sha256"))
    if weight_bytes is None:
        return None, weight_status
    actual = str(payload.get("weights_sha256", ""))
    if pinned and pinned.strip().lower() != actual.lower():
        return None, "hash-mismatch"
    try:
        from safetensors.numpy import load_file
    except ImportError:
        return None, "torch-missing"
    try:
        state = _read_state(weight_bytes, load_file)
    except Exception:
        log.warning("trace model weights could not be read", exc_info=True)
        return None, "bad-format"
    return (
        TraceArchive(
            hidden=int(payload["hidden"]),
            backend=str(payload.get("backend") or "plain"),
            state=state,
            device=str(payload.get("device") or "cpu"),
            device_requested=str(payload.get("device_requested") or payload.get("device") or "cpu"),
            fell_back=bool(payload.get("fell_back", False)),
            gnn_fell_back=bool(payload.get("gnn_fell_back", False)),
            versions={str(key): value for key, value in (payload.get("versions") or {}).items()},
            weights_sha256=actual,
        ),
        "ok",
    )
