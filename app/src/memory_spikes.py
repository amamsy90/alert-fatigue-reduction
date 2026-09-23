"""Detect memory-spike episodes (replaces alert-text grouping).

Each injected GAIA fault is a short high-memory program on one service. Week 1
sets the normal level of each memory series. A later run of points above
mean + k * std, on that same service, is one episode. Log lines are not
clustered by their text. A line belongs to an episode only when its service
and time fall inside the spike.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import Config
from .entities import Fault

SERVICES = (
    "dbservice1",
    "dbservice2",
    "logservice1",
    "logservice2",
    "mobservice1",
    "mobservice2",
    "webservice1",
    "webservice2",
    "redisservice1",
    "redisservice2",
)


@dataclass
class Spike:
    series_id: str
    service: str
    start: pd.Timestamp
    end: pd.Timestamp
    # Largest height above normal inside the episode, in the metric's unit.
    peak_rise: float = 0.0


def service_in_name(name: str) -> str | None:
    lowered = name.lower()
    if "memory" not in lowered and "mem" not in lowered:
        return None
    for service in SERVICES:
        if service in lowered:
            return service
    return None


def read_metric_csv(path: str | Path, service: str, series_id: str | None = None) -> pd.DataFrame:
    frame = pd.read_csv(path)
    raw = frame["timestamp"]
    if pd.api.types.is_numeric_dtype(raw) and float(raw.max()) > 1e12:
        timestamps = pd.to_datetime(raw, unit="ms", utc=True)
    else:
        timestamps = pd.to_datetime(raw, utc=True)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "value": frame["value"].astype(float),
            "service": service,
            "series_id": series_id or Path(path).stem,
        }
    )


def load_metric_folder(path: str | Path) -> pd.DataFrame:
    folder = Path(path)
    frames = []
    for csv_path in sorted(folder.rglob("*.csv")):
        service = service_in_name(csv_path.name)
        if service is None:
            continue
        frames.append(read_metric_csv(csv_path, service, series_id=service))
    if not frames:
        raise FileNotFoundError(f"No memory metric csv files in {folder}")
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(["series_id", "timestamp"]).sort_values("timestamp")
    return combined


def detect_spikes(
    metrics: pd.DataFrame,
    config: Config | None = None,
    fit_until: pd.Timestamp | None = None,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
) -> list[Spike]:
    """Thresholds come from points before `fit_until`. Spikes are read after `detect_from`.

    By default both bounds are the end of week 1, so week 2 is scored with a
    week-1 normal level. Pass an earlier `fit_until` and a `detect_until` to
    find spikes inside week 1 without using week 2.
    """
    config = config or Config()
    metrics = metrics.sort_values("timestamp")
    origin = metrics["timestamp"].min()
    split_at = origin + pd.Timedelta(days=config.split_day)
    if fit_until is None:
        fit_until = split_at
    if detect_from is None:
        detect_from = split_at
    spikes: list[Spike] = []

    for series_id, part in metrics.groupby("series_id"):
        train = part[part["timestamp"] < fit_until]
        test = part[part["timestamp"] >= detect_from]
        if detect_until is not None:
            test = test[test["timestamp"] < detect_until]
        if train.empty or test.empty:
            continue
        mean = float(train["value"].mean())
        std = float(train["value"].std(ddof=0))
        cutoff = mean + config.memory_spike_k * std
        hot = test[test["value"] > cutoff]
        heights = pd.Series((hot["value"] - mean).to_numpy(), index=hot["timestamp"])
        service = str(part["service"].iloc[0])
        spikes.extend(_episodes(heights, str(series_id), service, config))
    return spikes


def detect_jumps(
    metrics: pd.DataFrame,
    config: Config | None = None,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
) -> list[Spike]:
    """A point is hot when it sits `memory_jump_bytes` above the median of the
    previous `memory_jump_window_minutes` on the same series.

    Unlike mean + k * std this needs no training week, so a service whose normal
    level drifts (a restart, a slow leak) is not flagged for days.
    """
    config = config or Config()
    window = f"{config.memory_jump_window_minutes}min"
    spikes: list[Spike] = []
    for series_id, part in metrics.groupby("series_id"):
        series = part.sort_values("timestamp").set_index("timestamp")["value"]
        baseline = series.rolling(window, closed="left").median()
        rise = series - baseline
        hot = rise[rise > config.memory_jump_bytes]
        if detect_from is not None:
            hot = hot[hot.index >= detect_from]
        if detect_until is not None:
            hot = hot[hot.index < detect_until]
        service = str(part["service"].iloc[0])
        spikes.extend(_episodes(hot, str(series_id), service, config))
    return spikes


def _episodes(
    heights: pd.Series, series_id: str, service: str, config: Config
) -> list[Spike]:
    """Group hot points no more than the gap apart; keep runs of min_points or more.

    `heights` is indexed by timestamp; its values give each episode's peak_rise.
    """
    gap = pd.Timedelta(minutes=config.memory_gap_minutes)
    episodes: list[Spike] = []
    cluster_start = cluster_end = previous = None
    cluster_count = 0
    peak = 0.0
    for timestamp, height in heights.items():
        if previous is None or timestamp - previous > gap:
            if cluster_count >= config.memory_min_points:
                episodes.append(Spike(series_id, service, cluster_start, cluster_end, peak))
            cluster_start = cluster_end = timestamp
            cluster_count = 1
            peak = float(height)
        else:
            cluster_end = timestamp
            cluster_count += 1
            peak = max(peak, float(height))
        previous = timestamp
    if cluster_count >= config.memory_min_points:
        episodes.append(Spike(series_id, service, cluster_start, cluster_end, peak))
    return episodes


def score_spikes(spikes: list[Spike], faults: list[Fault]) -> dict[str, float]:
    """A fault is caught when a spike on its service overlaps the injection window."""
    detected: set[str] = set()
    false_spikes = 0
    for spike in spikes:
        hits = [
            fault
            for fault in faults
            if fault.component == spike.service
            and spike.start <= fault.end
            and spike.end >= fault.start
        ]
        if hits:
            detected.update(fault.fault_id for fault in hits)
        else:
            false_spikes += 1
    return {
        "spikes": float(len(spikes)),
        "faults": float(len(faults)),
        "faults_detected": float(len(detected)),
        "detection_rate": (len(detected) / len(faults)) if faults else 0.0,
        "false_spikes": float(false_spikes),
    }
