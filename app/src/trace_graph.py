"""Causal service-call graphs built from spans.

RCAEval's trace files are not in the log and metric download (`tools/fetch_rcaeval_re23.py`
keeps `logs.parquet` and `metrics.parquet` only). This module reads a span table
when one is present. A span counts only when its recorded time is strictly
before the decision time. The root-cause service is a label for scoring, not
a feature.

RCAEval `traces.parquet` columns (Online Boutique and Train Ticket; Sock Shop
has no traces) are accepted under their published names: `time`, `traceID`,
`spanID`, `parentSpanID`, `serviceName`, `duration`, `statusCode`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

NODE_FEATURES = ("log_calls", "error_rate", "log_duration", "error_jump", "latency_jump")
EDGE_FEATURES = ("log_calls", "error_rate", "latency_jump")
_OK_STATUS = frozenset({"", "0", "0.0", "ok", "unset", "200"})

_ALIASES = {
    "time": ("time", "timestamp", "starttime", "start_time"),
    "trace_id": ("traceid", "trace_id", "traceid"),
    "span_id": ("spanid", "span_id"),
    "parent_span_id": ("parentspanid", "parent_span_id", "parentspan"),
    "service": ("servicename", "service", "service_name"),
    "duration": ("duration", "duration_ms", "latency"),
    "status": ("statuscode", "status_code", "status", "response_code", "responsecode"),
}


def _pick(columns: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in columns:
            return columns[name]
    return None


def _is_error(value: object) -> int:
    if value is None:
        return 0
    try:
        if pd.isna(value):
            return 0
    except TypeError:
        pass
    text = str(value).strip().lower()
    return 0 if text in _OK_STATUS else 1


def normalize_spans(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per span with `time`, `service`, `duration`, `error`, and ids.

    `time` becomes UTC. A numeric time is unix seconds, which is what RCAEval
    stores. Duration is kept in the file's own unit; jump features are ratios.
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["time", "trace_id", "span_id", "parent_span_id", "service", "duration", "error"])
    lookup = {str(column).strip().lower(): str(column) for column in frame.columns}
    time_col = _pick(lookup, _ALIASES["time"])
    service_col = _pick(lookup, _ALIASES["service"])
    if time_col is None or service_col is None:
        raise ValueError("span table needs a time column and a service column")
    raw_time = frame[time_col]
    if np.issubdtype(raw_time.dtype, np.number):
        stamps = pd.to_datetime(raw_time, unit="s", utc=True)
    else:
        stamps = pd.to_datetime(raw_time, utc=True)
    duration_col = _pick(lookup, _ALIASES["duration"])
    status_col = _pick(lookup, _ALIASES["status"])
    duration = (
        pd.to_numeric(frame[duration_col], errors="coerce").fillna(0.0).to_numpy(dtype=np.float64)
        if duration_col
        else np.zeros(len(frame), dtype=np.float64)
    )
    if status_col:
        error = np.asarray([_is_error(value) for value in frame[status_col]], dtype=np.int64)
    else:
        error = np.zeros(len(frame), dtype=np.int64)

    def _text(name: str) -> np.ndarray:
        column = _pick(lookup, _ALIASES[name])
        if column is None:
            return np.asarray([""] * len(frame))
        return frame[column].fillna("").astype(str).to_numpy()

    out = pd.DataFrame(
        {
            "time": stamps,
            "trace_id": _text("trace_id"),
            "span_id": _text("span_id"),
            "parent_span_id": _text("parent_span_id"),
            "service": frame[service_col].fillna("").astype(str).to_numpy(),
            "duration": duration,
            "error": error,
        }
    )
    return out[out["service"].astype(str).str.len() > 0].reset_index(drop=True)


@dataclass
class CallGraph:
    """One causal window. Node order is the sorted service name."""

    services: list[str]
    node_x: np.ndarray
    edge_index: np.ndarray
    edge_attr: np.ndarray
    error_jump: dict[str, float]
    latency_jump: dict[str, float]


def _rates(part: pd.DataFrame) -> dict[str, tuple[int, float, float]]:
    """service -> (calls, error rate, mean duration)."""
    out: dict[str, tuple[int, float, float]] = {}
    if part.empty:
        return out
    for service, rows in part.groupby("service", sort=True):
        calls = int(len(rows))
        if calls == 0:
            continue
        out[str(service)] = (
            calls,
            float(rows["error"].mean()),
            float(rows["duration"].mean()),
        )
    return out


def build_graph(spans: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> CallGraph:
    """Graph of spans with `start <= time < end`.

    Baseline rates use only spans with `time < start`. A parent span is an
    edge only when that parent was recorded before `end`.
    """
    frame = spans if "error" in spans.columns else normalize_spans(spans)
    if frame.empty:
        return CallGraph([], np.zeros((0, 5)), np.zeros((2, 0), dtype=np.int64), np.zeros((0, 3)), {}, {})
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    window = frame[(frame["time"] >= start_ts) & (frame["time"] < end_ts)]
    past = frame[frame["time"] < start_ts]
    visible = frame[frame["time"] < end_ts]
    current = _rates(window)
    history = _rates(past)
    services = sorted(current)
    error_jump: dict[str, float] = {}
    latency_jump: dict[str, float] = {}
    rows: list[list[float]] = []
    for service in services:
        calls, error_rate, mean_duration = current[service]
        past_row = history.get(service)
        if past_row is None:
            err_j, lat_j = 0.0, 0.0
        else:
            err_j = error_rate - past_row[1]
            lat_j = 0.0 if past_row[0] == 0 else (mean_duration - past_row[2]) / max(past_row[2], 1e-6)
        error_jump[service] = float(err_j)
        latency_jump[service] = float(lat_j)
        rows.append(
            [
                float(np.log1p(calls)),
                float(error_rate),
                float(np.log1p(max(mean_duration, 0.0))),
                float(err_j),
                float(lat_j),
            ]
        )
    node_x = np.asarray(rows, dtype=np.float64) if rows else np.zeros((0, 5), dtype=np.float64)
    edge_index, edge_attr = _edges(window, visible, past, services)
    return CallGraph(services, node_x, edge_index, edge_attr, error_jump, latency_jump)


def _edges(
    window: pd.DataFrame,
    visible: pd.DataFrame,
    past: pd.DataFrame,
    services: list[str],
) -> tuple[np.ndarray, np.ndarray]:
    if window.empty or not services:
        return np.zeros((2, 0), dtype=np.int64), np.zeros((0, 3), dtype=np.float64)
    parent_service: dict[str, str] = {}
    if not visible.empty and "span_id" in visible.columns:
        ordered = visible.sort_values("time")
        for span_id, service in zip(ordered["span_id"], ordered["service"]):
            key = str(span_id)
            if key and key not in parent_service:
                parent_service[key] = str(service)
    index = {service: position for position, service in enumerate(services)}
    calls: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for parent_id, service, duration, error in zip(
        window["parent_span_id"], window["service"], window["duration"], window["error"]
    ):
        parent = parent_service.get(str(parent_id), "")
        child = str(service)
        if not parent or parent == child or parent not in index or child not in index:
            continue
        calls.setdefault((parent, child), []).append((float(duration), int(error)))
    if not calls:
        return np.zeros((2, 0), dtype=np.int64), np.zeros((0, 3), dtype=np.float64)
    past_pairs = _pair_means(past, parent_service)
    src: list[int] = []
    dst: list[int] = []
    attr: list[list[float]] = []
    for (parent, child), samples in sorted(calls.items()):
        count = len(samples)
        error_rate = sum(item[1] for item in samples) / count
        mean_duration = sum(item[0] for item in samples) / count
        past_mean = past_pairs.get((parent, child))
        latency = 0.0 if past_mean is None else (mean_duration - past_mean) / max(past_mean, 1e-6)
        features = [float(np.log1p(count)), float(error_rate), float(latency)]
        # Both directions, so a callee sees its caller and the caller sees the callee.
        src.extend((index[parent], index[child]))
        dst.extend((index[child], index[parent]))
        attr.extend((features, features))
    return (
        np.asarray([src, dst], dtype=np.int64),
        np.asarray(attr, dtype=np.float64),
    )


def _pair_means(past: pd.DataFrame, parent_service: dict[str, str]) -> dict[tuple[str, str], float]:
    totals: dict[tuple[str, str], list[float]] = {}
    if past.empty:
        return {}
    for parent_id, service, duration in zip(past["parent_span_id"], past["service"], past["duration"]):
        parent = parent_service.get(str(parent_id), "")
        child = str(service)
        if not parent or parent == child:
            continue
        totals.setdefault((parent, child), []).append(float(duration))
    return {pair: float(np.mean(values)) for pair, values in totals.items() if values}


def baseline_service(graph: CallGraph) -> str | None:
    """Service with the largest error-rate or latency jump. Ties break on name.

    The score is `max(error_jump, latency_jump)`. Naming a caller is not a
    catch: the caller has to win this comparison on its own numbers.
    """
    if not graph.services:
        return None
    return min(
        graph.services,
        key=lambda service: (-max(graph.error_jump[service], graph.latency_jump[service]), service),
    )


def root_cause_hit(predicted: str | None, component: str) -> bool:
    """A catch counts only when the named service is the root-cause service."""
    return bool(predicted) and predicted == component
