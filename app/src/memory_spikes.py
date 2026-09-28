"""Metric episodes: jumps (the main detector), monitoring gaps, and the old rule.

A jump is a run of points that sit well above the median of the previous
window on the same series. It is *confirmed* at its `memory_min_points`-th hot
point; `confirm` and `rise_at_confirm` are all a live queue may use.
`end` and `peak_rise` are only known afterwards and are kept for reporting.

A gap is a series with no points for `gap_minutes` or more: the queue cannot
see a fault there, so the gap itself is shown to on-call.

`detect_spikes` (training mean + k * std) is the first rule, kept for comparison.
Log lines are not clustered by their text. A line belongs to an episode only when
its service and time fall inside it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .entities import Fault

# Points a series needs before robust mode scores it (the spread floor needs history).
ROBUST_WARMUP_POINTS = 10

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
    # Known only after the episode ends: do not use it to decide what to show.
    peak_rise: float = 0.0
    # Time of the point that confirms the episode, and the rise known by then.
    confirm: pd.Timestamp | None = None
    rise_at_confirm: float = 0.0
    kind: str = "jump"  # "jump" | "gap" | "rule" | "leak"
    # Recent median at the confirming point (the "normal" level the rise is measured from).
    level_at_confirm: float = 0.0
    # Largest spacing between hot points of this episode (see _gap_for).
    grace: pd.Timedelta | None = None

    @property
    def shown_at(self) -> pd.Timestamp:
        """When a live queue can show this item."""
        return self.confirm if self.confirm is not None else self.start

    def open_at(self, t: pd.Timestamp) -> bool:
        """Does a live system at time `t` see this episode as shown and still open?

        Open means its last hot point up to `t` is within `grace` of `t`, so
        the episode has not closed yet. That uses only points up to `t`: hot
        points are at most `grace` apart, so `end >= t - grace` holds exactly
        when some hot point lies in [t - grace, t].
        """
        if t < self.shown_at:
            return False
        grace = self.grace if self.grace is not None else pd.Timedelta(0)
        return self.end >= t - grace


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


def median_step(index: pd.DatetimeIndex) -> pd.Timedelta:
    """Typical sampling interval of a series (30 s for GAIA, 5 min for NAB, ...)."""
    if len(index) < 2:
        return pd.Timedelta(minutes=1)
    return pd.Series(index).diff().median()


def jump_scores(series: pd.Series, config: Config) -> tuple[pd.Series, pd.Series]:
    """(score, baseline) per point. A point is hot when score > 0.

    absolute  score = rise - memory_jump_bytes            (rise in metric units)
    relative  score = rise - memory_jump_ratio * |median|
    robust    score = |rise| / scale - robust_z, scale = rolling IQR / 1.349
              (unit-free: works for counts, temperatures, latency, money ...)

    `rise` is the value minus the median of the previous window. With
    `jump_direction` = "down" or "both", drops count too (robust mode only).
    """
    window = f"{config.memory_jump_window_minutes}min"
    roll = series.rolling(window, closed="left")
    baseline = roll.median()
    rise = series - baseline
    mode = config.memory_jump_mode
    if mode == "absolute":
        return rise - config.memory_jump_bytes, baseline
    if mode == "relative":
        return rise - config.memory_jump_ratio * baseline.abs(), baseline
    if mode != "robust":
        raise ValueError(f"memory_jump_mode must be absolute, relative or robust, not {mode!r}")
    iqr = (roll.quantile(0.75) - roll.quantile(0.25)) / 1.349
    # Floor for flat windows: 5% of the spread seen so far (earlier points only,
    # so a later outlier cannot change an earlier score). NaN during warm-up,
    # which leaves the first points unscored instead of hot.
    floor = (series.expanding(min_periods=ROBUST_WARMUP_POINTS).std(ddof=0).shift(1) * 0.05).clip(lower=1e-9)
    scale = pd.Series(np.fmax(iqr.to_numpy(), floor.to_numpy()), index=series.index)
    scale[floor.isna()] = np.nan
    if config.jump_direction == "up":
        signed = rise
    elif config.jump_direction == "down":
        signed = -rise
    else:
        signed = rise.abs()
    return signed / scale - config.robust_z, baseline


def detect_jumps(
    metrics: pd.DataFrame,
    config: Config | None = None,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
) -> list[Spike]:
    """A point is hot when it sits `memory_jump_bytes` above the median of the
    previous `memory_jump_window_minutes` on the same series (absolute mode),
    `memory_jump_ratio` times that median above it (relative mode), or
    `robust_z` robust standard deviations away from it (robust mode, any metric).

    Unlike mean + k * std this needs no training week, so a service whose normal
    level drifts (a restart, a slow leak) is not flagged for days.
    """
    config = config or Config()
    spikes: list[Spike] = []
    for series_id, part in metrics.groupby("series_id"):
        series = part.sort_values("timestamp").set_index("timestamp")["value"]
        score, baseline = jump_scores(series, config)
        hot = score[score > 0]
        if config.memory_jump_mode in ("absolute", "relative"):
            heights = (series - baseline)[hot.index]  # rise in metric units, as before
        else:
            heights = hot + config.robust_z  # robust deviations
        if detect_from is not None:
            heights = heights[heights.index >= detect_from]
        if detect_until is not None:
            heights = heights[heights.index < detect_until]
        service = str(part["service"].iloc[0])
        gap = _gap_for(series.index, config)
        for ep in _episodes(heights, str(series_id), service, config, gap=gap):
            level = baseline.get(ep.confirm)
            ep.level_at_confirm = float(level) if level is not None and pd.notna(level) else 0.0
            spikes.append(ep)
    return spikes


def _gap_for(index: pd.DatetimeIndex, config: Config) -> pd.Timedelta:
    """Largest spacing between hot points of one episode: the configured minutes,
    or 2.5 samples when `memory_gap_minutes` is 0 (auto, for any sampling rate)."""
    if config.memory_gap_minutes and config.memory_gap_minutes > 0:
        return pd.Timedelta(minutes=config.memory_gap_minutes)
    return median_step(index) * 2.5


def detect_leaks(
    metrics: pd.DataFrame,
    config: Config | None = None,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
) -> list[Spike]:
    """Slow rises: the jump rule with a long median window (`leak_window_minutes`)
    and a longer run of hot points (`leak_min_points`). Episodes that overlap a
    fast jump already shown on the same series are dropped (the jump covers them).
    Returns [] when `leak_window_minutes` is None."""
    config = config or Config()
    if not config.leak_window_minutes:
        return []
    from dataclasses import replace

    slow = replace(
        config,
        memory_jump_mode="absolute",
        memory_jump_window_minutes=config.leak_window_minutes,
        memory_jump_bytes=config.leak_rise_bytes,
        memory_min_points=config.leak_min_points,
    )
    fast = detect_jumps(metrics, config, detect_from=detect_from, detect_until=detect_until)
    leaks = detect_jumps(metrics, slow, detect_from=detect_from, detect_until=detect_until)
    out = []
    for leak in leaks:
        # Drop the leak only if a fast jump on the same series was already shown
        # by the leak's confirmation and overlapped it up to then. A jump that
        # arrives later cannot un-show a leak a live queue already showed.
        if any(
            j.series_id == leak.series_id
            and j.shown_at <= leak.shown_at
            and j.end >= leak.start
            for j in fast
        ):
            continue
        leak.kind = "leak"
        out.append(leak)
    return out


def _episodes(
    heights: pd.Series,
    series_id: str,
    service: str,
    config: Config,
    gap: pd.Timedelta | None = None,
) -> list[Spike]:
    """Group hot points no more than the gap apart; keep runs of min_points or more.

    `heights` is indexed by timestamp; its values give each episode's rise.
    """
    gap = gap if gap is not None else pd.Timedelta(minutes=config.memory_gap_minutes)
    need = max(1, config.memory_min_points)
    episodes: list[Spike] = []
    run: list[tuple[pd.Timestamp, float]] = []

    def _close() -> None:
        if len(run) >= need:
            episodes.append(
                Spike(
                    series_id,
                    service,
                    run[0][0],
                    run[-1][0],
                    max(h for _, h in run),
                    confirm=run[need - 1][0],
                    rise_at_confirm=max(h for _, h in run[:need]),
                    grace=gap,
                )
            )

    previous = None
    for timestamp, height in heights.items():
        if previous is not None and timestamp - previous > gap:
            _close()
            run = []
        run.append((timestamp, float(height)))
        previous = timestamp
    _close()
    return episodes


def detect_gaps(
    metrics: pd.DataFrame,
    config: Config | None = None,
    detect_from: pd.Timestamp | None = None,
    detect_until: pd.Timestamp | None = None,
    now: pd.Timestamp | None = None,
) -> list[Spike]:
    """One item per silence of `gap_minutes` or more; silences that start close
    together on several services are merged into one item (service "a,b,c").

    The item is shown `gap_minutes` after the last point (that is when a live
    system knows data stopped). `end` is when data came back. A silence still
    going at `now` (default: the last point of any series, or `detect_until`)
    is reported with `end = now`: a dead feed must not wait for data to return.

    Merging is causal: a later silence within `gap_group_minutes` is attached
    to the item already shown instead of taking a new slot. `end` and the
    merged service list are reporting fields, filled in afterwards.
    """
    config = config or Config()
    limit = pd.Timedelta(minutes=config.gap_minutes)
    if now is None:
        now = detect_until if detect_until is not None else (
            metrics["timestamp"].max() if not metrics.empty else None
        )
    raw: list[tuple[pd.Timestamp, pd.Timestamp, str]] = []

    def _keep(last_seen: pd.Timestamp) -> bool:
        shown = last_seen + limit
        if detect_from is not None and shown < detect_from:
            return False
        return detect_until is None or shown < detect_until

    for _, part in metrics.groupby("series_id"):
        times = part["timestamp"].sort_values().reset_index(drop=True)
        if times.empty:
            continue
        step = times.diff()
        service = str(part["service"].iloc[0])
        for i in step.index[step > limit]:
            last_seen, back = times[i - 1], times[i]
            if _keep(last_seen):
                raw.append((last_seen, back, service))
        last_seen = times.iloc[-1]
        if now is not None and now - last_seen > limit and _keep(last_seen):
            raw.append((last_seen, now, service))
    raw.sort()
    merged: list[Spike] = []
    window = pd.Timedelta(minutes=config.gap_group_minutes)
    for last_seen, back, service in raw:
        if merged and last_seen - merged[-1].start <= window:
            item = merged[-1]
            if service not in item.service.split(","):
                item.service = f"{item.service},{service}"
            item.end = max(item.end, back)
            continue
        merged.append(
            Spike(
                series_id="gap",
                service=service,
                start=last_seen,
                end=back,
                peak_rise=0.0,
                confirm=last_seen + limit,
                rise_at_confirm=0.0,
                kind="gap",
            )
        )
    return merged


def faults_in_gaps(gaps: list[Spike], faults: list[Fault]) -> set[str]:
    """Faults that start while their service's metrics were silent."""
    covered: set[str] = set()
    for gap in gaps:
        services = set(gap.service.split(","))
        for fault in faults:
            if fault.component in services and gap.start <= fault.start <= gap.end:
                covered.add(fault.fault_id)
    return covered


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
