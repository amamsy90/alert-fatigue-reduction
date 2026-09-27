"""Train, tune and evaluate the queue on ANY timestamped data — not only GAIA.

A user brings up to three tables (CSV or DataFrame):

  metrics     timestamp, value[, series]      one row per sample; or wide (one column per series)
  events      timestamp[, source, severity, message]   alerts, notifications, log lines (optional)
  incidents   start[, end, series, id]        past problems = labels (optional, needed to score)

Column names are detected (`timestamp`/`time`/`date`/…, `value`/`count`/…,
`series`/`service`/`host`/`sensor`/…). Everything is split by time per series:
the first `train_fraction` of each series trains and tunes, the rest is scored
once. Every system, baselines included, is tuned on the training part with the
same cost (10 x missed incident + 1 x false item) and scored with the same
causal daily-budget queue (src/queue.py).

`evaluate()` returns plain dicts, so the web app, the CLI and notebooks all
show the same report.
"""
from __future__ import annotations

import io
import itertools
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .baseline import baseline_decisions
from .config import Config, day_of
from .entities import Alert, Fault
from .memory_spikes import Spike, _episodes, _gap_for, detect_gaps, jump_scores, median_step
from .queue import (
    alert_items,
    build_queue,
    oracle_ceiling,
    score_queue,
    spike_hits,
    spike_items,
    tune_confirm_thresholds,
)

TIME_NAMES = ("timestamp", "time", "datetime", "date", "ts", "@timestamp", "event_time", "created_at", "start_time",
              "when", "occurred_at", "sent_at")
VALUE_NAMES = ("value", "count", "metric", "y", "val", "measurement", "reading", "amount")
SERIES_NAMES = ("series", "series_id", "service", "source", "host", "entity", "sensor", "device", "name",
                "metric_name", "component", "app", "channel", "topic", "region", "site", "store", "location",
                "country", "endpoint", "queue", "user", "account")
START_NAMES = ("start", "begin", "start_time", "from", "started_at", "timestamp", "time")
END_NAMES = ("end", "stop", "end_time", "to", "ended_at", "resolved_at")
SEVERITY_NAMES = ("severity", "level", "priority", "status")
MESSAGE_NAMES = ("message", "text", "msg", "title", "summary", "description", "body")


# ---------------------------------------------------------------------------
# Loading with column detection


def _read(obj) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    if isinstance(obj, (bytes, bytearray)):
        return pd.read_csv(io.BytesIO(obj))
    return pd.read_csv(obj)


def _pick(columns, names, required: str | None = None, exclude=()) -> str | None:
    lowered = {c.lower().strip(): c for c in columns if c not in exclude}
    for name in names:
        if name in lowered:
            return lowered[name]
    for name in names:  # substring match, e.g. "event_timestamp"; short names ("ts", "y") are exact only
        if len(name) < 4:
            continue
        for low, original in lowered.items():
            if name in low:
                return original
    if required:
        raise ValueError(f"Could not find a {required} column (looked for: {', '.join(names[:6])} …)")
    return None


def _time_column(raw: pd.DataFrame, names=TIME_NAMES, exclude=()) -> str:
    """A named time column, else the first column whose values parse as dates."""
    named = _pick(raw.columns, names, exclude=exclude)
    if named is not None:
        return named
    for c in raw.columns:
        if c in exclude:
            continue
        sample = raw[c].dropna().head(20)
        if sample.empty or pd.api.types.is_bool_dtype(sample):
            continue
        if pd.api.types.is_numeric_dtype(sample):
            if float(sample.min()) > 1e9:  # Unix seconds or milliseconds
                return c
            continue
        try:
            pd.to_datetime(sample, utc=True, format="mixed")
            return c
        except (ValueError, TypeError):
            continue
    raise ValueError("Could not find a time column (a column named time/timestamp/date, or values that look like dates)")


def _to_utc(values: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(values):
        top = float(values.max())
        unit = "ms" if top > 1e12 else "s"
        return pd.to_datetime(values, unit=unit, utc=True)
    return pd.to_datetime(values, utc=True, format="mixed")


def _overlap_column(raw: pd.DataFrame, known: set[str] | None, exclude=()) -> str | None:
    """A text column whose values are mostly known series names (e.g. 'region')."""
    if not known:
        return None
    best, share = None, 0.0
    for c in raw.columns:
        if c in exclude or pd.api.types.is_numeric_dtype(raw[c]):
            continue
        values = raw[c].dropna().astype(str)
        if values.empty:
            continue
        hit = float(values.isin(known).mean())
        if hit > share:
            best, share = c, hit
    return best if share >= 0.5 else None


def load_metrics(obj, default_series: str = "series") -> tuple[pd.DataFrame, dict]:
    """Long or wide metric table -> columns timestamp, value, service, series_id."""
    raw = _read(obj)
    if raw.empty:
        raise ValueError("The metrics file is empty")
    t = _time_column(raw)
    numeric = [c for c in raw.columns if c != t and pd.api.types.is_numeric_dtype(raw[c])]
    text = [c for c in raw.columns if c != t and not pd.api.types.is_numeric_dtype(raw[c])]
    v = _pick(raw.columns, VALUE_NAMES, exclude=(t,))
    if v is not None and not pd.api.types.is_numeric_dtype(raw[v]):
        v = None
    if v is None and len(numeric) == 1 and text:
        v = numeric[0]  # e.g. time, orders, region: one number per row plus a label
    s = _pick(raw.columns, SERIES_NAMES, exclude=(t, v))
    if s is None and v is not None and len(text) == 1:
        s = text[0]
    if v is None and len(numeric) >= 1:
        # wide: one numeric column per series
        long = raw.melt(id_vars=[t], value_vars=numeric, var_name="__series", value_name="__value")
        t_col, v_col, s_col, layout = t, "__value", "__series", "wide"
        raw = long
    else:
        if v is None:
            raise ValueError("Could not find a numeric value column in the metrics file")
        t_col, v_col, s_col, layout = t, v, s, "long"
    out = pd.DataFrame(
        {
            "timestamp": _to_utc(raw[t_col]),
            "value": pd.to_numeric(raw[v_col], errors="coerce"),
            "service": raw[s_col].astype(str) if s_col else default_series,
        }
    ).dropna(subset=["timestamp", "value"])
    out["series_id"] = out["service"]
    out = out.drop_duplicates(["series_id", "timestamp"]).sort_values("timestamp").reset_index(drop=True)
    mapping = {"layout": layout, "timestamp": t, "value": v if layout == "long" else numeric,
               "series": s if layout == "long" else "(one column per series)"}
    return out, mapping


def load_events(obj, known_series: set[str] | None = None) -> tuple[list[Alert], dict]:
    raw = _read(obj)
    if raw.empty:
        return [], {}
    t = _time_column(raw)
    s = _pick(raw.columns, SERIES_NAMES, exclude=(t,)) or _overlap_column(raw, known_series, exclude=(t,))
    sev = _pick(raw.columns, SEVERITY_NAMES, exclude=(t, s))
    msg = _pick(raw.columns, MESSAGE_NAMES, exclude=(t, s, sev))
    ident = next((c for c in raw.columns if c.lower() in ("alert_id", "event_id", "id", "fingerprint")), None)
    kind = next((c for c in raw.columns if c.lower() in ("alert_type", "type", "alertname", "category")), None)
    stamps = _to_utc(raw[t])
    alerts = []
    for i, (ts, rec) in enumerate(zip(stamps, raw.to_dict("records"))):
        if pd.isna(ts):
            continue
        severity = str(rec.get(sev, "info") if sev else "info").lower()
        message = str(rec.get(msg, "") if msg else "")
        source = str(rec.get(s)) if s else (next(iter(known_series)) if known_series and len(known_series) == 1 else "events")
        aid = str(rec[ident]) if ident and pd.notna(rec.get(ident)) else f"e{i:07d}"
        atype = str(rec[kind]) if kind and pd.notna(rec.get(kind)) else severity
        alerts.append(Alert(aid, ts, source, atype, severity, message[:300]))
    alerts.sort(key=lambda a: a.timestamp)
    return alerts, {"timestamp": t, "source": s, "severity": sev, "message": msg}


def load_incidents(obj, default_minutes: float = 10.0, known_series: set[str] | None = None) -> tuple[list[Fault], dict]:
    """Incidents with no end get `default_minutes`; with no series they apply to every series."""
    raw = _read(obj)
    if raw.empty:
        return [], {}
    st = _time_column(raw, START_NAMES)
    en = _pick(raw.columns, END_NAMES, exclude=(st,))
    s = _pick(raw.columns, SERIES_NAMES, exclude=(st, en)) or _overlap_column(raw, known_series, exclude=(st, en))
    starts = _to_utc(raw[st])
    ends = _to_utc(raw[en]) if en else starts + pd.Timedelta(minutes=default_minutes)
    faults: list[Fault] = []
    for i, (a, b) in enumerate(zip(starts, ends)):
        if pd.isna(a):
            continue
        b = a + pd.Timedelta(minutes=default_minutes) if pd.isna(b) or b < a else b
        targets = [str(raw[s].iloc[i])] if s else sorted(known_series or {"*"})
        for target in targets:
            faults.append(Fault(f"i{i:05d}" + ("" if s or len(targets) == 1 else f"_{target}"), a, b, target, "incident"))
    faults.sort(key=lambda f: f.start)
    return faults, {"start": st, "end": en or f"(start + {default_minutes:g} min)", "series": s or "(all series)"}


# ---------------------------------------------------------------------------
# Train / test split per series


def split_points(metrics: pd.DataFrame, train_fraction: float) -> dict[str, pd.Timestamp]:
    cuts = {}
    for sid, part in metrics.groupby("series_id"):
        lo, hi = part["timestamp"].min(), part["timestamp"].max()
        cuts[str(sid)] = lo + (hi - lo) * train_fraction
    return cuts


def _in_part(ts: pd.Timestamp, cut: pd.Timestamp | None, part: str) -> bool:
    if cut is None:
        return part == "test"
    return ts < cut if part == "train" else ts >= cut


def _faults_part(faults: list[Fault], cuts: dict, part: str) -> list[Fault]:
    return [f for f in faults if _in_part(f.start, cuts.get(f.component), part)]


def _spikes_part(spikes: list[Spike], cuts: dict, part: str) -> list[Spike]:
    return [s for s in spikes if _in_part(s.shown_at, cuts.get(s.series_id), part)]


# ---------------------------------------------------------------------------
# Detectors with a precomputed score, so tuning is cheap


@dataclass
class DetectorSetting:
    kind: str                 # "jump" (robust jump rule), "delta" (rate rule), "static" (quantile)
    window_points: int = 12   # jump: median window; delta: look-back
    z: float = 5.0            # jump / delta: robust units; static: quantile
    min_points: int = 2
    direction: str = "both"
    # Per series, learned on the training part only:
    #   static: (low, high) quantile levels
    #   delta:  (0, scale)
    #   jump:   (confirm rise,) — episodes below it are not shown; missing series use `z`
    levels: dict | None = None
    # Jump only: a hot threshold (jump size, robust units) learned per series on
    # the training part where that series had enough incidents; others use `z`.
    z_by_series: dict | None = None

    def label(self) -> str:
        if self.kind == "static":
            return f"static q={self.z:g}, {self.direction}"
        per = f", size per series for {len(self.z_by_series)}" if self.z_by_series else ""
        return f"{self.kind} window={self.window_points} pts, z={self.z:g}, points={self.min_points}, {self.direction}{per}"


def _scores(metrics: pd.DataFrame, setting: DetectorSetting, cuts: dict) -> dict[str, tuple[pd.Series, pd.Timedelta]]:
    """Per series: a signed-by-direction deviation score (robust units) and the episode gap."""
    out = {}
    for sid, part in metrics.groupby("series_id"):
        series = part.set_index("timestamp")["value"].sort_index()
        step = median_step(series.index)
        gap = step * 2.5
        if setting.kind == "jump":
            cfg = Config(memory_jump_mode="robust", robust_z=0.0, jump_direction=setting.direction,
                         memory_jump_window_minutes=max(1, int(round(setting.window_points * step / pd.Timedelta(minutes=1)))))
            score, _ = jump_scores(series, cfg)
        elif setting.kind == "delta":
            diff = series - series.shift(setting.window_points)
            if setting.levels and str(sid) in setting.levels:
                scale = float(setting.levels[str(sid)][1])
            else:
                train = series[series.index < cuts.get(str(sid), series.index.max())]
                q = train.diff().abs()
                scale = max(float(q.quantile(0.75) - q.quantile(0.25)) / 1.349, float(train.std(ddof=0)) * 0.05, 1e-9)
            signed = diff if setting.direction == "up" else -diff if setting.direction == "down" else diff.abs()
            score = signed / scale
        else:  # static: distance beyond the training quantile, in raw units
            if setting.levels and str(sid) in setting.levels:
                lo, hi = setting.levels[str(sid)]
            else:
                train = series[series.index < cuts.get(str(sid), series.index.max())]
                hi = float(train.quantile(setting.z)) if len(train) else float(series.max())
                lo = float(train.quantile(1 - setting.z)) if len(train) else float(series.min())
            up = (series - hi).clip(lower=0)
            down = (lo - series).clip(lower=0)
            score = up if setting.direction == "up" else down if setting.direction == "down" else np.maximum(up, down)
            score = pd.Series(np.where(score > 0, score + 1e-12, -1.0), index=series.index)
        out[str(sid)] = (score, gap)
    return out


def _episodes_from(scores, threshold: float, min_points: int, kind: str,
                   z_by_series: dict | None = None) -> list[Spike]:
    cfg = Config(memory_min_points=min_points)
    spikes = []
    for sid, (score, gap) in scores.items():
        thr = float(z_by_series[sid]) if z_by_series and sid in z_by_series and kind != "static" else threshold
        hot = score[score > thr]
        for ep in _episodes(hot, sid, sid, cfg, gap=gap):
            ep.kind = "jump" if kind == "jump" else "rule"
            spikes.append(ep)
    return spikes


def _cost(spikes: list[Spike], faults: list[Fault], config: Config) -> tuple[float, int, int]:
    by_service: dict[str, list[Fault]] = {}
    for f in faults:
        by_service.setdefault(f.component, []).append(f)
    caught: set[str] = set()
    false = 0
    for s in spikes:
        hits = spike_hits(s, by_service)
        if hits:
            caught |= {f.fault_id for f in hits}
        else:
            false += 1
    return config.miss_cost * (len(faults) - len(caught)) + config.false_alarm_cost * false, len(caught), false


GRIDS = {
    "jump": {"window_points": (12, 48, 288), "z": (3, 4, 5, 6, 8, 12), "min_points": (1, 2, 3)},
    "delta": {"window_points": (1, 3, 12), "z": (3, 4, 5, 6, 8, 12), "min_points": (1, 2, 3)},
    "static": {"window_points": (0,), "z": (0.99, 0.995, 0.999), "min_points": (1, 2, 3)},
}


def tune(metrics: pd.DataFrame, train_faults: list[Fault], cuts: dict, kind: str, config: Config,
         directions=("up", "both")) -> tuple[DetectorSetting, dict]:
    """Grid-search one detector family on the training part (lowest cost wins).

    Without training incidents there is nothing to fit: returns a conservative
    default (the defaults the web app uses) and says so.
    """
    grid = GRIDS[kind]
    default = DetectorSetting(kind, grid["window_points"][0 if kind != "jump" else 1],
                              {"jump": 6.0, "delta": 6.0, "static": 0.999}[kind], 2, "up")
    if not train_faults:
        return default, {"tuned": False, "reason": "no incidents in the training part"}
    best = None
    tried = 0
    for window, direction in itertools.product(grid["window_points"], directions):
        base = DetectorSetting(kind, window, 0.0, 1, direction)
        scores = _scores(metrics, base, cuts)
        # only the training part is looked at while tuning
        train_scores = {sid: (sc[sc.index < cuts.get(sid, sc.index.max())], gap) for sid, (sc, gap) in scores.items()}
        for z, mp in itertools.product(grid["z"], grid["min_points"]):
            tried += 1
            threshold = 0.0 if kind == "static" else z
            setting = DetectorSetting(kind, window, z, mp, direction)
            sc_use = train_scores if kind != "static" else {
                sid: (s, g) for sid, (s, g) in _scores(metrics, setting, cuts).items()
            }
            if kind == "static":
                sc_use = {sid: (s[s.index < cuts.get(sid, s.index.max())], g) for sid, (s, g) in sc_use.items()}
            spikes = _episodes_from(sc_use, threshold, mp, kind)
            cost, caught, false = _cost(spikes, train_faults, config)
            key = (cost, -caught, false)
            if best is None or key < best[0]:
                best = (key, setting, caught, false)
    (cost, _, _), setting, caught, false = best
    return setting, {"tuned": True, "tried": tried, "train_cost": cost, "train_caught": caught,
                     "train_incidents": len(train_faults), "train_false": false}


MIN_INCIDENTS_PER_SERIES = 3


def tune_jump_size_by_series(metrics: pd.DataFrame, train_faults: list[Fault], cuts: dict,
                             setting: DetectorSetting, config: Config,
                             min_incidents: int = MIN_INCIDENTS_PER_SERIES) -> dict[str, float]:
    """Per-series jump size (the hot threshold `z`) for the jump rule.

    Window, direction and min_points stay global. A series gets its own size only
    when it has at least `min_incidents` training incidents and a size on the grid
    beats the global one on that series alone (same cost: 10 x miss + false).
    One size does not fit all: on GAIA a 0.25 GB step is invisible at 0.5 GB.
    """
    if setting.kind != "jump" or not train_faults:
        return {}
    by_series: dict[str, list[Fault]] = {}
    for f in train_faults:
        by_series.setdefault(str(f.component), []).append(f)
    base = DetectorSetting("jump", setting.window_points, 0.0, 1, setting.direction)
    scores = _scores(metrics, base, cuts)
    out: dict[str, float] = {}
    for sid, faults in by_series.items():
        if len(faults) < min_incidents or sid not in scores:
            continue
        sc, gap = scores[sid]
        train = {sid: (sc[sc.index < cuts.get(sid, sc.index.max())], gap)}
        def cost_at(z: float) -> tuple:
            c, caught, false = _cost(_episodes_from(train, z, setting.min_points, "jump"), faults, config)
            return (c, -caught, false)
        global_cost = cost_at(setting.z)
        best_z, best = setting.z, global_cost
        for z in GRIDS["jump"]["z"]:
            c = cost_at(float(z))
            if c < best:
                best_z, best = float(z), c
        if best < global_cost:
            out[sid] = best_z
    return out


def detect(metrics: pd.DataFrame, setting: DetectorSetting, cuts: dict) -> list[Spike]:
    threshold = 0.0 if setting.kind == "static" else setting.z
    spikes = _episodes_from(_scores(metrics, setting, cuts), threshold, setting.min_points, setting.kind,
                            setting.z_by_series)
    # Jump only: `levels` holds a confirm rise learned on the training window
    # (one number per series). Static and delta use `levels` as a scale, not a gate.
    if setting.kind == "jump" and setting.levels:
        kept = []
        for spike in spikes:
            gate = setting.levels.get(str(spike.series_id))
            if gate is None or spike.rise_at_confirm >= float(gate[0]):
                kept.append(spike)
        return kept
    return spikes


# ---------------------------------------------------------------------------
# The report


@dataclass
class Report:
    summary: dict
    mapping: dict
    settings: dict
    table: list[dict]
    misses: dict
    checks: dict
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _chance(spikes: list[Spike], faults: list[Fault], spans: dict[str, tuple], seed: int = 0) -> float:
    """Detection if every episode were moved to a uniformly random time inside its
    own series' test span (same count, same lengths). The line to beat."""
    if not spikes or not faults:
        return 0.0
    rng = np.random.default_rng(seed)
    rates = []
    for _ in range(50):
        moved = []
        for s in spikes:
            lo, hi = spans.get(s.series_id, (s.start, s.end))
            length = s.end - s.start
            room = max((hi - lo - length).total_seconds(), 0.0)
            start = lo + pd.Timedelta(seconds=float(rng.uniform(0, room)))
            moved.append(Spike(s.series_id, s.service, start, start + length, kind=s.kind, confirm=start))
        rates.append(_cost(moved, faults, Config())[1] / len(faults))
    return float(np.mean(rates))


def evaluate(
    metrics,
    incidents=None,
    events=None,
    *,
    budgets=(5, 10, 20),
    train_fraction: float = 0.3,
    config: Config | None = None,
    default_incident_minutes: float = 10.0,
    day_offset_hours: float = 0.0,
) -> Report:
    """Tune every system on the first `train_fraction` of each series, score the rest."""
    config = config or Config()
    config = replace(config, day_offset_hours=day_offset_hours)
    frame, m_map = load_metrics(metrics)
    series = set(frame["series_id"].astype(str))
    faults, i_map = load_incidents(incidents, default_incident_minutes, series) if incidents is not None else ([], {})
    alerts, e_map = load_events(events, series) if events is not None else ([], {})
    faults = [f for f in faults if f.component in series]
    cuts = split_points(frame, train_fraction)
    train_faults, test_faults = _faults_part(faults, cuts, "train"), _faults_part(faults, cuts, "test")
    notes: list[str] = []

    settings, fits = {}, {}
    for kind in ("jump", "delta", "static"):
        settings[kind], fits[kind] = tune(frame, train_faults, cuts, kind, config)
    # Per-series jump size, then a per-series confirm gate, both fit on the training part and frozen.
    if fits["jump"].get("tuned"):
        z_by = tune_jump_size_by_series(frame, train_faults, cuts, settings["jump"], config)
        if z_by:
            settings["jump"].z_by_series = z_by
            fits["jump"]["z_by_series"] = dict(z_by)
            notes.append(
                f"Jump size learned per series on the training part for {len(z_by)} series with "
                f"{MIN_INCIDENTS_PER_SERIES}+ incidents (kept only where it beats the global size); "
                "the others use the global size."
            )
        train_jumps = _spikes_part(detect(frame, settings["jump"], cuts), cuts, "train")
        global_thr, by_series = tune_confirm_thresholds(train_jumps, train_faults, config)
        if by_series:
            settings["jump"].levels = {sid: (float(thr),) for sid, thr in by_series.items()}
            fits["jump"]["confirm_by_series"] = {sid: float(thr) for sid, thr in by_series.items()}
            fits["jump"]["confirm_global"] = None if global_thr is None else float(global_thr)
            notes.append(
                f"Jump confirm level learned per series on the training part for {len(by_series)} "
                "series; the others keep the global jump threshold. The log classifier is separate."
            )
    detected = {kind: _spikes_part(detect(frame, s, cuts), cuts, "test") for kind, s in settings.items()}
    # The live queue uses the detector family with the lowest training cost.
    tuned_kinds = [k for k in settings if fits[k].get("tuned")]
    chosen = min(tuned_kinds, key=lambda k: fits[k]["train_cost"]) if tuned_kinds else "jump"
    notes.append(f"Detector chosen for your queue: {chosen} ({settings[chosen].label()}), "
                 f"lowest cost on the training part." if tuned_kinds else
                 "Detector for your queue: robust jump rule with default settings (nothing to tune on).")
    gaps = [g for g in detect_gaps(frame, replace(config, gap_minutes=_auto_gap_minutes(frame, config)))
            if any(_in_part(g.shown_at, cuts.get(sv), "test") for sv in g.service.split(","))]

    test_alerts = [a for a in alerts if _in_part(a.timestamp, cuts.get(a.source, min(cuts.values(), default=None)), "test")]
    if faults and alerts:
        from .ingestion import align_alerts_to_faults

        align_alerts_to_faults(test_alerts, test_faults)
    test_start = min(cuts.values()) if cuts else frame["timestamp"].min()
    test_end = frame["timestamp"].max() + pd.Timedelta(seconds=1)
    n_days = max(1, int((day_of(test_end, config) - day_of(test_start, config)).days) + 1)

    rows = []
    for k in budgets:
        cfg = replace(config, budget_k=int(k))
        systems = {
            "static_rule": build_queue(spike_items(detected["static"], cfg), cfg),
            "rate_rule": build_queue(spike_items(detected["delta"], cfg), cfg),
            "jump_rule": build_queue(spike_items(detected["jump"], cfg), cfg),
            "your_queue": build_queue(
                spike_items(detected[chosen], cfg) + spike_items(gaps, cfg)
                + (alert_items(test_alerts, baseline_decisions(test_alerts, cfg)) if test_alerts else []),
                cfg,
            ),
        }
        if test_alerts:
            systems = {"events_severity": build_queue(
                alert_items(test_alerts, baseline_decisions(test_alerts, cfg)), replace(cfg, alert_share=1.0)
            ), **systems}
        ceiling = oracle_ceiling(test_faults, cfg) if test_faults else 0.0
        for name, shown in systems.items():
            row = score_queue(name, shown, test_faults, n_days, cfg) if test_faults else {
                "queue": name, "budget_k": k, "faults": 0, "faults_caught": 0, "detection_rate": None,
                "items_per_day": round(sum(not i.attached for i in shown) / n_days, 2),
            }
            row["oracle_ceiling"] = round(ceiling, 4)
            rows.append(row)

    misses = {}
    checks = {}
    if test_faults:
        k = 20 if 20 in budgets else budgets[-1]
        cfg = replace(config, budget_k=int(k))
        misses = _miss_causes(frame, test_faults, detected[chosen], build_queue(spike_items(detected[chosen], cfg), cfg))
        spans = {sid: (cuts[sid], p["timestamp"].max()) for sid, p in frame.groupby("series_id")}
        checks["chance_detection"] = round(_chance(detected[chosen], test_faults, spans), 4)
        caught_uncapped = _cost(detected[chosen], test_faults, config)[1]
        checks["detection_no_budget"] = round(caught_uncapped / len(test_faults), 4)
        if checks["chance_detection"] > 0.5 * max(checks["detection_no_budget"], 1e-9):
            notes.append("Incident windows are wide compared with the gaps between items: a random detector would "
                         "catch a large share. Judge the systems against the chance line, not against zero.")
    else:
        notes.append("No incidents given: the report shows item counts only. Add past incidents (start, end, series) "
                     "to get detection rates, tuned settings and a comparison with the rules.")
    if not train_faults and faults:
        notes.append("No incidents fall in the training part: settings are defaults, not tuned. Lower train_fraction "
                     "or add older incidents.")

    step = pd.Series([median_step(p.set_index("timestamp").index) for _, p in frame.groupby("series_id")]).median()
    summary = {
        "series": len(series),
        "points": int(len(frame)),
        "start": frame["timestamp"].min().isoformat(),
        "end": frame["timestamp"].max().isoformat(),
        "sampling": str(step),
        "events": len(alerts),
        "incidents": len(faults),
        "train_incidents": len(train_faults),
        "test_incidents": len(test_faults),
        "test_days": n_days,
        "train_fraction": train_fraction,
    }
    return Report(
        summary=summary,
        mapping={"metrics": m_map, "incidents": i_map, "events": e_map},
        settings={**{kind: {**asdict(s), "label": s.label(), **fits[kind]} for kind, s in settings.items()},
                  "chosen": chosen},
        table=rows,
        misses=misses,
        checks=checks,
        notes=notes,
    )


def _auto_gap_minutes(frame: pd.DataFrame, config: Config) -> int:
    """Silence worth reporting: 10 minutes, or 5 samples for slowly sampled data."""
    step = pd.Series([median_step(p.set_index("timestamp").index) for _, p in frame.groupby("series_id")]).median()
    return int(max(config.gap_minutes, 5 * step / pd.Timedelta(minutes=1)))


def _miss_causes(frame, faults, jumps, shown) -> dict:
    times = {sid: p["timestamp"].sort_values() for sid, p in frame.groupby("series_id")}
    by_service: dict[str, list[Fault]] = {}
    for f in faults:
        by_service.setdefault(f.component, []).append(f)
    caught = {f.fault_id for it in shown if it.kind != "gap" for f in spike_hits(it.payload, by_service)}
    overlapped = {f.fault_id for j in jumps for f in spike_hits(j, by_service)}
    counts = {"no_data": 0, "no_detection": 0, "budget_full": 0}
    for f in faults:
        if f.fault_id in caught:
            continue
        arr = times.get(f.component)
        has = arr is not None and bool(((arr >= f.start) & (arr <= f.end)).any())
        if not has:
            counts["no_data"] += 1
        elif f.fault_id not in overlapped:
            counts["no_detection"] += 1
        else:
            counts["budget_full"] += 1
    return counts


def learned_levels(metrics: pd.DataFrame, setting: DetectorSetting, cuts: dict) -> dict:
    """Per-series numbers a saved static or rate rule needs to score new data."""
    levels = {}
    for sid, part in metrics.groupby("series_id"):
        series = part.set_index("timestamp")["value"].sort_index()
        train = series[series.index < cuts.get(str(sid), series.index.max())]
        if not len(train):
            continue
        if setting.kind == "static":
            levels[str(sid)] = (float(train.quantile(1 - setting.z)), float(train.quantile(setting.z)))
        elif setting.kind == "delta":
            q = train.diff().abs()
            scale = max(float(q.quantile(0.75) - q.quantile(0.25)) / 1.349, float(train.std(ddof=0)) * 0.05, 1e-9)
            levels[str(sid)] = (0.0, scale)
    return levels


def live_setting(metrics_obj, report: "Report | dict", train_fraction: float | None = None) -> dict:
    """The chosen detector as a JSON-able dict the web app saves and scores with."""
    data = report.to_dict() if isinstance(report, Report) else report
    chosen = data["settings"]["chosen"]
    raw = {k: v for k, v in data["settings"][chosen].items() if k in DetectorSetting.__dataclass_fields__}
    setting = DetectorSetting(**raw)
    frame, _ = load_metrics(metrics_obj)
    cuts = split_points(frame, train_fraction if train_fraction is not None else data["summary"]["train_fraction"])
    if setting.kind in ("static", "delta"):
        setting.levels = learned_levels(frame, setting, cuts)
    return {
        "detector": asdict(setting),
        "label": setting.label(),
        "tuned": bool(data["settings"][chosen].get("tuned")),
        "sampling": data["summary"]["sampling"],
        "gap_minutes": _auto_gap_minutes(frame, Config()),
        "source": data["summary"],
    }


def setting_from_dict(d: dict) -> DetectorSetting:
    raw = dict(d["detector"] if "detector" in d else d)
    if raw.get("levels"):
        raw["levels"] = {k: tuple(v) for k, v in raw["levels"].items()}
    return DetectorSetting(**{k: v for k, v in raw.items() if k in DetectorSetting.__dataclass_fields__})
