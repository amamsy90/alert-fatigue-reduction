"""Optional multivariate metric model. Off unless ``METRIC_MODEL`` is set.

One causal TCN per service reads every channel the frame actually has
(memory, and cpu / latency / error when those columns exist) and scores a
window by one-step forecast error. The published GAIA loader stores one
docker-memory series per service, so on that extract the channel list is
``memory`` only. A frame with a ``metric`` column, or with distinct
``series_id`` values, is scored jointly.

The model proposes extra ``rule`` items. It does not page, and it does not
replace the jump rule. Windows, the channel list, and the normaliser use only
points at or before the decision time. Training windows end before the split.
Weights are a safetensors state dict plus a JSON sidecar, each with a SHA-256.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .entities import Fault
from .memory_spikes import Spike
from .queue import QueueItem, build_queue, n_days_between, score_queue
from .weight_io import load_bytes, save_bytes

log = logging.getLogger(__name__)

_ON = frozenset({"1", "true", "yes", "on"})
FORMAT = "oncall-metric-tcn-v1"
WEIGHTS_NAME = "model.safetensors"
META_NAME = "model.json"
MIN_WINDOWS = 8


def metric_model_enabled() -> bool:
    return os.environ.get("METRIC_MODEL", "").strip().lower() in _ON


def metric_model_directory() -> Path | None:
    raw = os.environ.get("METRIC_MODEL_DIR", "").strip()
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


def _channel_labels(part: pd.DataFrame, service: str) -> pd.Series:
    """Name each row's channel. GAIA's single series (id == service) is ``memory``."""
    if "metric" in part.columns and part["metric"].notna().any():
        return part["metric"].astype(str)
    if "series_id" not in part.columns:
        return pd.Series(["memory"] * len(part), index=part.index)
    raw = part["series_id"].astype(str)
    unique = set(raw.unique())
    if unique <= {service, "memory", f"{service}:memory", f"{service}_memory"}:
        return pd.Series(["memory"] * len(part), index=part.index)
    prefix = service + ":"

    def _short(value: str) -> str:
        return value[len(prefix) :] if value.startswith(prefix) else value

    return raw.map(_short)


def channel_frame(
    metrics: pd.DataFrame,
    *,
    freq: str,
    until: pd.Timestamp | None = None,
) -> dict[str, tuple[pd.DatetimeIndex, np.ndarray, list[str]]]:
    """Per service: bin-close time, ``values (T, C)``, channel names.

    A bin is the half-open interval ending at its label. Points at or after
    ``until`` are dropped first, so a later row cannot change an earlier bin.
    Forward-fill uses only earlier bins. Rows that are still missing a channel
    are dropped.
    """
    if metrics is None or metrics.empty:
        return {}
    frame = metrics.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    if until is not None:
        frame = frame[frame["timestamp"] < _utc(until)]
    if frame.empty or "service" not in frame.columns:
        return {}
    delta = pd.Timedelta(freq)
    out: dict[str, tuple[pd.DatetimeIndex, np.ndarray, list[str]]] = {}
    for service, part in frame.groupby("service", sort=True):
        labels = _channel_labels(part, str(service))
        closed = part["timestamp"].dt.floor(freq) + delta
        table = part.assign(_channel=labels.to_numpy(), _close=closed)
        if until is not None:
            table = table[table["_close"] <= _utc(until)]
        if table.empty:
            continue
        names = sorted(set(table["_channel"].astype(str)))
        pivot = (
            table.pivot_table(index="_close", columns="_channel", values="value", aggfunc="mean")
            .reindex(columns=names)
            .sort_index()
            .ffill()
        )
        pivot = pivot.dropna(how="any")
        if pivot.empty:
            continue
        index = pd.DatetimeIndex(pivot.index)
        if index.tz is None:
            index = index.tz_localize("UTC")
        out[str(service)] = (index, pivot.to_numpy(dtype=np.float64), names)
    return out


def fit_norm(
    aligned: dict[str, tuple[pd.DatetimeIndex, np.ndarray, list[str]]],
    channels: list[str],
    train_end: pd.Timestamp,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-channel mean and std from bins that close strictly before ``train_end``."""
    columns: list[np.ndarray] = []
    boundary = _utc(train_end)
    for index, values, names in aligned.values():
        mask = index < boundary
        if not mask.any():
            continue
        columns.append(_reindex(values[mask], names, channels, fill=np.nan))
    if not columns:
        raise ValueError("no metric bins close before the split")
    stacked = np.concatenate(columns, axis=0)
    mean = np.nanmean(stacked, axis=0)
    std = np.nanstd(stacked, axis=0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    std = np.where(np.isfinite(std) & (std >= 1e-6), std, 1.0)
    return mean.astype(np.float64), std.astype(np.float64)


def _reindex(values: np.ndarray, names: list[str], channels: list[str], fill: float | np.ndarray) -> np.ndarray:
    lookup = {name: index for index, name in enumerate(names)}
    out = np.empty((values.shape[0], len(channels)), dtype=np.float64)
    fill_row = np.broadcast_to(np.asarray(fill, dtype=np.float64), (len(channels),))
    for column, channel in enumerate(channels):
        if channel in lookup:
            out[:, column] = values[:, lookup[channel]]
        else:
            out[:, column] = fill_row[column]
    return out


def training_channels(metrics: pd.DataFrame, freq: str, train_end: pd.Timestamp) -> list[str]:
    names: set[str] = set()
    for _index, _values, channels in channel_frame(metrics, freq=freq, until=train_end).values():
        names.update(channels)
    return sorted(names)


def _overlaps(start: pd.Timestamp, end: pd.Timestamp, service: str, faults: list[Fault]) -> bool:
    for fault in faults:
        if fault.component == service and start <= fault.end and end >= fault.start:
            return True
    return False


def _windows(
    aligned: dict[str, tuple[pd.DatetimeIndex, np.ndarray, list[str]]],
    channels: list[str],
    mean: np.ndarray,
    std: np.ndarray,
    faults: list[Fault],
    train_end: pd.Timestamp,
    window: int,
    stride: int,
    *,
    normal_only: bool,
) -> tuple[np.ndarray, np.ndarray]:
    contexts: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    boundary = _utc(train_end)
    scale = np.where(std == 0, 1.0, std)
    for service, (index, values, names) in aligned.items():
        grid = _reindex(values, names, channels, fill=mean)
        for row in range(window, len(index), stride):
            if index[row] >= boundary:
                break
            if normal_only and _overlaps(index[row - window], index[row], service, faults):
                continue
            context = grid[row - window : row]
            target = grid[row]
            if not np.isfinite(context).all() or not np.isfinite(target).all():
                continue
            contexts.append(((context - mean) / scale).T)  # (C, W)
            targets.append((target - mean) / scale)
    if not contexts:
        empty = np.zeros((0, len(channels), window), dtype=np.float32)
        return empty, np.zeros((0, len(channels)), dtype=np.float32)
    return (
        np.stack(contexts).astype(np.float32),
        np.stack(targets).astype(np.float32),
    )


def score_matrix(
    values: np.ndarray,
    index: pd.DatetimeIndex,
    forecast,
    mean: np.ndarray,
    std: np.ndarray,
    window: int,
    stride: int,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> list[tuple[pd.Timestamp, float]]:
    """Forecast-error at each stride. The context is the ``window`` bins before the target.

    ``values`` must already contain only bins knowable at ``end``. The target
    bin is known when it closes; the context is strictly earlier.
    """
    if len(index) <= window:
        return []
    scale = np.where(std == 0, 1.0, std)
    begin = _utc(start) if start is not None else None
    stop = _utc(end) if end is not None else None
    rows = []
    contexts = []
    targets = []
    for row in range(window, len(index), stride):
        if begin is not None and index[row] < begin:
            continue
        if stop is not None and index[row] >= stop:
            break
        context = values[row - window : row]
        target = values[row]
        if not np.isfinite(context).all() or not np.isfinite(target).all():
            continue
        rows.append(row)
        contexts.append(((context - mean) / scale).T)
        targets.append((target - mean) / scale)
    if not rows:
        return []
    predicted = np.asarray(forecast(np.stack(contexts).astype(np.float32)), dtype=np.float64)
    actual = np.stack(targets)
    error = np.mean((predicted - actual) ** 2, axis=1)
    return [(index[row], float(value)) for row, value in zip(rows, error)]


@dataclass
class MetricArchive:
    channels: list[str]
    mean: np.ndarray
    std: np.ndarray
    threshold: float
    window: int
    stride: int
    freq: str
    hidden: int
    kernel: int
    dilations: tuple[int, ...]
    state: dict[str, np.ndarray]
    device: str
    device_requested: str
    fell_back: bool
    normal_only: bool
    versions: dict[str, str | None] = field(default_factory=dict)
    weights_sha256: str = ""

    def forecaster(self):
        from .metric_tcn import make_forecaster, resolve_torch_device

        device, _fell_back = resolve_torch_device(self.device)
        return make_forecaster(
            self.state,
            n_channels=len(self.channels),
            hidden=self.hidden,
            kernel=self.kernel,
            dilations=self.dilations,
            device=device,
        )

    def score(
        self,
        metrics: pd.DataFrame,
        *,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
    ) -> dict[str, list[tuple[pd.Timestamp, float]]]:
        forecast = self.forecaster()
        aligned = channel_frame(metrics, freq=self.freq, until=end)
        scored: dict[str, list[tuple[pd.Timestamp, float]]] = {}
        for service, (index, values, names) in aligned.items():
            grid = _reindex(values, names, self.channels, fill=self.mean)
            scored[service] = score_matrix(
                grid, index, forecast, self.mean, self.std, self.window, self.stride, start, end
            )
        return scored


def _subsample(contexts: np.ndarray, targets: np.ndarray, max_windows: int) -> tuple[np.ndarray, np.ndarray]:
    if len(contexts) <= max_windows:
        return contexts, targets
    pick = np.linspace(0, len(contexts) - 1, max_windows).astype(int)
    return contexts[pick], targets[pick]


def fit_metric_model(
    metrics: pd.DataFrame,
    faults: list[Fault],
    train_end: pd.Timestamp,
    *,
    freq: str = "30s",
    window: int = 32,
    stride: int = 10,
    hidden: int = 16,
    kernel: int = 3,
    dilations: tuple[int, ...] = (1, 2, 4),
    epochs: int = 5,
    seed: int = 42,
    device: str = "cpu",
    max_windows: int = 4096,
) -> tuple[MetricArchive | None, str]:
    """Train on bins that close before ``train_end``. Returns ``(archive, status)``."""
    if not torch_stack_available():
        return None, "torch-missing"
    from .metric_tcn import resolve_torch_device, train_state

    channels = training_channels(metrics, freq, train_end)
    if not channels:
        return None, "no-channels"
    aligned = channel_frame(metrics, freq=freq, until=train_end)
    try:
        mean, std = fit_norm(aligned, channels, train_end)
    except ValueError:
        return None, "no-channels"
    train_faults = [fault for fault in faults if fault.start < _utc(train_end)]
    contexts, targets = _windows(
        aligned, channels, mean, std, train_faults, train_end, window, stride, normal_only=True
    )
    normal_only = True
    if len(contexts) < MIN_WINDOWS:
        contexts, targets = _windows(
            aligned, channels, mean, std, train_faults, train_end, window, stride, normal_only=False
        )
        normal_only = False
    if len(contexts) < MIN_WINDOWS:
        return None, "too-few-windows"
    contexts, targets = _subsample(contexts, targets, max_windows)
    used, fell_back = resolve_torch_device(device)
    state = train_state(
        contexts,
        targets,
        n_channels=len(channels),
        hidden=hidden,
        kernel=kernel,
        dilations=dilations,
        epochs=epochs,
        seed=seed,
        device=used,
    )
    archive = MetricArchive(
        channels=channels,
        mean=mean,
        std=std,
        threshold=float("inf"),
        window=window,
        stride=stride,
        freq=freq,
        hidden=hidden,
        kernel=kernel,
        dilations=dilations,
        state=state,
        device=used,
        device_requested=device,
        fell_back=fell_back,
        normal_only=normal_only,
        versions={"torch": _version("torch"), "safetensors": _version("safetensors")},
    )
    return archive, "trained"


def proposals_from_scores(
    scores: dict[str, list[tuple[pd.Timestamp, float]]],
    threshold: float,
    existing: list[QueueItem],
    *,
    freq: str,
    stride: int,
) -> list[Spike]:
    """One ``rule`` item at a hot bin, unless a jump, leak or rule is already that close."""
    gap = pd.Timedelta(freq) * max(1, stride)
    taken: dict[str, list[pd.Timestamp]] = {}
    for item in existing:
        if item.kind in ("jump", "leak", "rule"):
            taken.setdefault(item.service, []).append(item.shown_at)
    proposals: list[Spike] = []
    for service, series in scores.items():
        nearby = taken.get(service, [])
        for stamp, score in series:
            if not np.isfinite(score) or score < threshold:
                continue
            if any(abs(stamp - other) <= gap for other in nearby):
                continue
            proposals.append(
                Spike(
                    series_id=f"metric-model:{service}",
                    service=service,
                    start=stamp,
                    end=stamp,
                    peak_rise=float(score),
                    confirm=stamp,
                    rise_at_confirm=float(score),
                    kind="rule",
                )
            )
    return proposals


def tune_metric_threshold(
    scores: dict[str, list[tuple[pd.Timestamp, float]]],
    items: list[QueueItem],
    faults: list[Fault],
    config: Config,
    *,
    freq: str,
    stride: int,
) -> float:
    """Training-window threshold. Proposing nothing is one of the candidates.

    Ties keep the higher threshold, so the model adds an item only when that
    lowers the training cost.
    """
    values = [score for series in scores.values() for _stamp, score in series if np.isfinite(score)]
    if not values or not faults:
        return float("inf")
    grid = list(np.unique(np.quantile(np.asarray(values, dtype=float), np.linspace(0, 0.95, 11))))
    grid.append(float(np.max(values)) + 1.0)
    if items:
        start = min(item.shown_at for item in items)
        end = max(item.shown_at for item in items) + pd.Timedelta(seconds=1)
    else:
        stamps = [stamp for series in scores.values() for stamp, _score in series]
        start, end = min(stamps), max(stamps) + pd.Timedelta(seconds=1)
    n_days = n_days_between(start, end, config)
    best: tuple[float, float] | None = None
    for threshold in grid:
        extra = proposals_from_scores(scores, float(threshold), items, freq=freq, stride=stride)
        proposed = list(items) + [QueueItem(spike.shown_at, spike.kind, spike.service, spike) for spike in extra]
        shown = build_queue(proposed, config)
        row = score_queue("tune", shown, faults, n_days, config)
        false_items = row["false_items_per_day"] * n_days
        cost = config.miss_cost * (len(faults) - row["faults_caught"]) + config.false_alarm_cost * false_items
        if best is None or cost < best[0] - 1e-6 or (abs(cost - best[0]) <= 1e-6 and float(threshold) > best[1]):
            best = (cost, float(threshold))
    return float("inf") if best is None else best[1]


def save_metric_model(archive: MetricArchive, directory: Path) -> str:
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
        "objective": "one-step-forecast-mse",
        "channels": list(archive.channels),
        "mean": [float(value) for value in archive.mean],
        "std": [float(value) for value in archive.std],
        "threshold": None if np.isinf(archive.threshold) else float(archive.threshold),
        "window": archive.window,
        "stride": archive.stride,
        "freq": archive.freq,
        "hidden": archive.hidden,
        "kernel": archive.kernel,
        "dilations": list(archive.dilations),
        "device": archive.device,
        "device_requested": archive.device_requested,
        "fell_back": archive.fell_back,
        "normal_only": archive.normal_only,
        "versions": archive.versions,
        "weights_sha256": weights_sha,
        "state_keys": sorted(state),
    }
    meta = json.dumps(payload, sort_keys=True).encode("utf-8")
    save_bytes(directory / META_NAME, meta)
    archive.weights_sha256 = weights_sha
    return weights_sha


def _read_state(blob: bytes, load_file) -> dict[str, np.ndarray]:
    with tempfile.NamedTemporaryFile(suffix=".safetensors") as handle:
        handle.write(blob)
        handle.flush()
        loaded = load_file(handle.name)
    return {key: np.asarray(value) for key, value in loaded.items()}


def load_metric_model(directory: Path, expected_sha: str | None = None) -> tuple[MetricArchive | None, str]:
    """Load a hash-pinned archive. Does not import torch (scoring does)."""
    meta_bytes, status = load_bytes(directory / META_NAME)
    if meta_bytes is None:
        return None, status
    try:
        payload = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "bad-format"
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        return None, "bad-format"
    pinned = expected_sha or os.environ.get("METRIC_MODEL_SHA256", "")
    weight_bytes, weight_status = load_bytes(directory / WEIGHTS_NAME, payload.get("weights_sha256"))
    if weight_bytes is None:
        return None, weight_status
    actual = payload.get("weights_sha256", "")
    if pinned and pinned.strip().lower() != str(actual).lower():
        return None, "hash-mismatch"
    try:
        from safetensors.numpy import load_file
    except ImportError:
        return None, "torch-missing"
    try:
        state = _read_state(weight_bytes, load_file)
    except Exception:
        log.warning("metric model weights could not be read", exc_info=True)
        return None, "bad-format"
    threshold = payload.get("threshold")
    archive = MetricArchive(
        channels=[str(name) for name in payload.get("channels", [])],
        mean=np.asarray(payload.get("mean", []), dtype=np.float64),
        std=np.asarray(payload.get("std", []), dtype=np.float64),
        threshold=float("inf") if threshold is None else float(threshold),
        window=int(payload["window"]),
        stride=int(payload["stride"]),
        freq=str(payload["freq"]),
        hidden=int(payload["hidden"]),
        kernel=int(payload["kernel"]),
        dilations=tuple(int(value) for value in payload.get("dilations", [])),
        state=state,
        device=str(payload.get("device") or "cpu"),
        device_requested=str(payload.get("device_requested") or payload.get("device") or "cpu"),
        fell_back=bool(payload.get("fell_back", False)),
        normal_only=bool(payload.get("normal_only", True)),
        versions={str(key): value for key, value in (payload.get("versions") or {}).items()},
        weights_sha256=str(actual),
    )
    return archive, "ok"
