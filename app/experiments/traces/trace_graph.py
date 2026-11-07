"""Causal service-call graphs built from spans.

RCAEval's trace files are not in the log and metric download (`tools/fetch_rcaeval_re23.py`
keeps `logs.parquet` and `metrics.parquet` only, plus `cases.parquet`). This
module reads a span table when one is present. A span counts only when its
recorded time is strictly before the decision time. The root-cause service is
a label for scoring, not a feature.

RCAEval `traces.parquet` (Online Boutique and Train Ticket; Sock Shop has no
traces) has a text `time` column such as ``21:24``. That clock is not a span
time. The timestamp is `startTimeMillis` or `startTime` (or another numeric
epoch column).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

NODE_FEATURES = ("log_calls", "error_rate", "log_duration", "error_jump", "latency_jump")
EDGE_FEATURES = ("log_calls", "error_rate", "latency_jump")
_OK_STATUS = frozenset({"", "0", "0.0", "ok", "unset", "200"})
# Clock-of-day text is not an epoch. startTimeMillis wins over that column.
_CLOCK = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_TIME_ORDER = (
    "starttimemillis",
    "start_time_millis",
    "starttimeunixnano",
    "starttimeunixmicro",
    "starttime",
    "start_time",
    "timestamp",
    "time",
)

_ALIASES = {
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


def _error_flags(series: pd.Series) -> np.ndarray:
    """Same rule as ``_is_error``, without a Python loop over the rows."""
    text = series.astype("string").str.strip().str.lower().fillna("")
    return (~text.isin(_OK_STATUS)).to_numpy(dtype=np.int64)


def _clock_column(series: pd.Series) -> bool:
    """True when the values are clock text such as ``21:24``, not an epoch."""
    if pd.api.types.is_numeric_dtype(series):
        return False
    text = series.astype("string").str.strip()
    sample = text.dropna()
    if sample.empty:
        return False
    sample = sample.head(32)
    hits = sum(1 for value in sample if _CLOCK.match(str(value)))
    return hits * 2 > len(sample)


def _numeric_epoch(series: pd.Series) -> pd.Series | None:
    """Numeric epoch values, including digit strings. Never raises on StringDtype."""
    if pd.api.types.is_numeric_dtype(series):
        numeric = pd.to_numeric(series, errors="coerce")
    else:
        numeric = pd.to_numeric(series.astype("string"), errors="coerce")
    if not numeric.notna().any():
        return None
    return numeric


def _epoch_unit(name: str, median: float) -> str:
    key = name.lower()
    if "nano" in key or median >= 1e17:
        return "ns"
    if "micro" in key or median >= 1e14:
        return "us"
    if "milli" in key or median >= 1e11:
        return "ms"
    return "s"


def _stamps_from(series: pd.Series, name: str) -> pd.Series | None:
    if _clock_column(series):
        return None
    numeric = _numeric_epoch(series)
    if numeric is not None:
        median = float(numeric.dropna().abs().median())
        stamps = pd.to_datetime(numeric, unit=_epoch_unit(name, median), utc=True, errors="coerce")
        if stamps.notna().any():
            return stamps
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    if parsed.notna().any():
        return parsed
    return None


def _span_stamps(frame: pd.DataFrame, lookup: dict[str, str]) -> pd.Series:
    """Prefer startTimeMillis / startTime. A ``21:24`` column is never used."""
    ordered: list[str] = []
    seen: set[str] = set()
    for name in _TIME_ORDER:
        column = lookup.get(name)
        if column and column not in seen:
            ordered.append(column)
            seen.add(column)
    for lower, column in lookup.items():
        if column in seen:
            continue
        if "time" in lower or "timestamp" in lower:
            ordered.append(column)
            seen.add(column)
    for column in ordered:
        stamps = _stamps_from(frame[column], column)
        if stamps is not None:
            return stamps
    raise ValueError(
        "span table has no usable timestamp; a clock column such as 21:24 is not a span time"
    )


def normalize_spans(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per span with `time`, `service`, `duration`, `error`, and ids.

    `time` becomes UTC. A numeric epoch is seconds unless the column is
    milliseconds (``startTimeMillis``) or the magnitude says otherwise.
    Duration is kept in the file's own unit; jump features are ratios.
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["time", "trace_id", "span_id", "parent_span_id", "service", "duration", "error"])
    lookup = {str(column).strip().lower(): str(column) for column in frame.columns}
    service_col = _pick(lookup, _ALIASES["service"])
    if service_col is None:
        raise ValueError("span table needs a time column and a service column")
    stamps = _span_stamps(frame, lookup)
    duration_col = _pick(lookup, _ALIASES["duration"])
    status_col = _pick(lookup, _ALIASES["status"])
    duration = (
        pd.to_numeric(frame[duration_col], errors="coerce").fillna(0.0).to_numpy(dtype=np.float64)
        if duration_col
        else np.zeros(len(frame), dtype=np.float64)
    )
    if status_col:
        error = _error_flags(frame[status_col])
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


def _build_graph_loops(spans: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> CallGraph:
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


def _as_utc(stamp: pd.Timestamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def _empty_graph() -> CallGraph:
    return CallGraph([], np.zeros((0, 5)), np.zeros((2, 0), dtype=np.int64), np.zeros((0, 3)), {}, {})


def _service_rates(part: pd.DataFrame) -> pd.DataFrame:
    """One row per service: calls, error rate, mean duration. Sorted by name."""
    if part.empty:
        return pd.DataFrame(columns=["calls", "error_rate", "mean_duration"])
    grouped = part.groupby("service", sort=True)
    rates = grouped.agg(calls=("error", "size"), error_rate=("error", "mean"), mean_duration=("duration", "mean"))
    rates.index = rates.index.map(str)
    return rates


def _parent_lookup(visible: pd.DataFrame) -> pd.DataFrame:
    """Earliest span id -> service. A repeated id keeps the earliest row."""
    columns = ["parent_span_id", "parent_service"]
    if visible.empty or "span_id" not in visible.columns:
        return pd.DataFrame(columns=columns)
    keep = visible.loc[visible["span_id"].astype(str).str.len() > 0, ["time", "span_id", "service"]]
    if keep.empty:
        return pd.DataFrame(columns=columns)
    keep = keep.sort_values("time", kind="mergesort")
    keep = keep.drop_duplicates("span_id", keep="first")
    lookup = keep.rename(columns={"span_id": "parent_span_id", "service": "parent_service"})
    lookup["parent_span_id"] = lookup["parent_span_id"].astype(str)
    lookup["parent_service"] = lookup["parent_service"].astype(str)
    return lookup[columns]


def _with_parent(part: pd.DataFrame, lookup: pd.DataFrame) -> pd.DataFrame:
    """Attach the parent service by merging parentSpanID onto spanID."""
    if part.empty:
        return part.assign(parent_service=pd.Series(dtype=str))
    frame = part.copy()
    frame["parent_span_id"] = frame["parent_span_id"].astype(str)
    frame["service"] = frame["service"].astype(str)
    if lookup.empty:
        frame["parent_service"] = ""
        return frame
    return frame.merge(lookup, on="parent_span_id", how="left")


def _pair_stats(part: pd.DataFrame, lookup: pd.DataFrame, services: set[str] | None) -> pd.DataFrame:
    """Call counts and sums for each parent -> child pair. ``services`` limits the window edges."""
    empty = pd.DataFrame(columns=["count", "duration_sum", "error_sum"])
    if part.empty:
        return empty
    frame = _with_parent(part, lookup)
    parent = frame["parent_service"].fillna("").astype(str)
    child = frame["service"].astype(str)
    mask = (parent != "") & (parent != child)
    if services is not None:
        mask = mask & parent.isin(services) & child.isin(services)
    frame = frame.loc[mask]
    if frame.empty:
        return empty
    frame = frame.assign(parent_service=parent.loc[mask], service=child.loc[mask])
    grouped = frame.groupby(["parent_service", "service"], sort=True)
    return grouped.agg(count=("duration", "size"), duration_sum=("duration", "sum"), error_sum=("error", "sum"))


def _graph_vectorized(window: pd.DataFrame, past: pd.DataFrame, visible: pd.DataFrame) -> CallGraph:
    current = _service_rates(window)
    if current.empty:
        return _empty_graph()
    history = _service_rates(past)
    services = [str(name) for name in current.index]
    error_jump: dict[str, float] = {}
    latency_jump: dict[str, float] = {}
    rows: list[list[float]] = []
    for service in services:
        calls = int(current.at[service, "calls"])
        error_rate = float(current.at[service, "error_rate"])
        mean_duration = float(current.at[service, "mean_duration"])
        if service not in history.index:
            err_j, lat_j = 0.0, 0.0
        else:
            past_calls = int(history.at[service, "calls"])
            past_error = float(history.at[service, "error_rate"])
            past_duration = float(history.at[service, "mean_duration"])
            err_j = error_rate - past_error
            lat_j = 0.0 if past_calls == 0 else (mean_duration - past_duration) / max(past_duration, 1e-6)
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
    node_x = np.asarray(rows, dtype=np.float64)
    lookup = _parent_lookup(visible)
    allowed = set(services)
    pairs = _pair_stats(window, lookup, allowed)
    if pairs.empty:
        return CallGraph(
            services,
            node_x,
            np.zeros((2, 0), dtype=np.int64),
            np.zeros((0, 3), dtype=np.float64),
            error_jump,
            latency_jump,
        )
    past_pairs = _pair_stats(past, lookup, None)
    past_mean: dict[tuple[str, str], float] = {}
    for key, row in past_pairs.iterrows():
        count = int(row["count"])
        if count:
            past_mean[(str(key[0]), str(key[1]))] = float(row["duration_sum"]) / count
    index = {service: position for position, service in enumerate(services)}
    src: list[int] = []
    dst: list[int] = []
    attr: list[list[float]] = []
    for key, row in pairs.iterrows():
        parent, child = str(key[0]), str(key[1])
        count = int(row["count"])
        error_rate = float(row["error_sum"]) / count
        mean_duration = float(row["duration_sum"]) / count
        previous = past_mean.get((parent, child))
        latency = 0.0 if previous is None else (mean_duration - previous) / max(previous, 1e-6)
        features = [float(np.log1p(count)), float(error_rate), float(latency)]
        src.extend((index[parent], index[child]))
        dst.extend((index[child], index[parent]))
        attr.extend((features, features))
    return CallGraph(
        services,
        node_x,
        np.asarray([src, dst], dtype=np.int64),
        np.asarray(attr, dtype=np.float64),
        error_jump,
        latency_jump,
    )


def build_graph(spans: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> CallGraph:
    """Graph of spans with `start <= time < end`.

    Baseline rates use only spans with `time < start`. A parent span is an
    edge only when that parent was recorded before `end`. Node and edge
    aggregates are pandas groupby; the parent service is a merge of
    ``parent_span_id`` onto ``span_id``.
    """
    frame = spans if "error" in spans.columns else normalize_spans(spans)
    if frame.empty:
        return _empty_graph()
    start_ts = _as_utc(start)
    end_ts = _as_utc(end)
    times = frame["time"]
    window = frame[(times >= start_ts) & (times < end_ts)]
    past = frame[times < start_ts]
    visible = frame[times < end_ts]
    return _graph_vectorized(window, past, visible)


def with_baseline_feature(graph: CallGraph) -> CallGraph:
    """Copy of ``graph`` with one extra node column, ``max(error_jump, latency_jump)``.

    The column is the score the non-learned baseline already uses, and it is
    known when the window closes. ``NODE_FEATURES`` stays the five default
    columns. The copy is only for the separate residual variant.
    """
    width = int(graph.node_x.shape[1]) if graph.node_x.ndim == 2 and graph.node_x.shape[1] else len(NODE_FEATURES)
    if not graph.services:
        return CallGraph(
            [],
            np.zeros((0, width + 1), dtype=np.float64),
            graph.edge_index,
            graph.edge_attr,
            {},
            {},
        )
    extra = np.asarray(
        [max(float(graph.error_jump[service]), float(graph.latency_jump[service])) for service in graph.services],
        dtype=np.float64,
    ).reshape(-1, 1)
    return CallGraph(
        list(graph.services),
        np.concatenate([np.asarray(graph.node_x, dtype=np.float64), extra], axis=1),
        graph.edge_index,
        graph.edge_attr,
        dict(graph.error_jump),
        dict(graph.latency_jump),
    )


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
