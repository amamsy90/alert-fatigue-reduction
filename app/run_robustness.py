"""Robustness replay: does the jump rule only work because every GAIA fault is 1 GB?

Synthetic faults of other shapes are added to the REAL GAIA memory series in
the test period, at random times at least an hour away from any real fault on
that service, and at points where the series has data. Then each detector is
run on the modified series and scored only on the synthetic faults.

Shapes (onset at t0):
  step_S      +S GB held for 10 minutes (S = 0.25, 0.5, 1, 2) — the GAIA shape at other sizes
  short_1g    +1 GB held for 1 minute (two or three samples)
  ramp_1g     +1 GB reached linearly over 30 minutes, then held 10 minutes (a fast leak)
  leak_1g     +1 GB reached linearly over 3 hours (a slow leak)
  control     nothing injected, 3 h window: how often a detector fires anyway

Detectors: the jump rule (0.5 GB over the 15-minute median, absolute), the same
with the week-1 confirmation threshold, relative mode (+30%), and the
Prometheus-style rate rule delta(mem[5m]) > 0.5 GB, and the jump rule plus the
optional slow-leak rule (3-hour median, +0.5 GB for 10 minutes). No daily budget here: the
question is whether the signal is detectable at all.

Usage:
    python run_robustness.py
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from src.baselines_metric import detect_delta
from src.config import Config
from src.experiment import load_gaia
from src.memory_spikes import detect_jumps, detect_leaks
from src.queue import tune_confirm_threshold

GB = 1e9
SHAPES = {
    "step_0.25g": ("step", 0.25 * GB, 10),
    "step_0.5g": ("step", 0.5 * GB, 10),
    "step_1g": ("step", 1.0 * GB, 10),
    "step_2g": ("step", 2.0 * GB, 10),
    "short_1g": ("step", 1.0 * GB, 1),
    "ramp_1g": ("ramp", 1.0 * GB, 30),
    "leak_1g": ("ramp", 1.0 * GB, 180),
    "control_none": ("ramp", 0.0, 180),  # nothing injected: background hit rate
}
N_PER_SERVICE = 12


def _inject(series: pd.Series, t0: pd.Timestamp, kind: str, size: float, minutes: int) -> tuple[pd.Series, pd.Timestamp]:
    out = series.copy()
    if kind == "step":
        end = t0 + pd.Timedelta(minutes=minutes)
        mask = (out.index >= t0) & (out.index < end)
        out[mask] += size
    else:  # ramp to `size` over `minutes`, then hold 10 minutes
        ramp_end = t0 + pd.Timedelta(minutes=minutes)
        end = ramp_end + pd.Timedelta(minutes=10)
        ramp = (out.index >= t0) & (out.index < ramp_end)
        frac = (out.index[ramp] - t0) / pd.Timedelta(minutes=minutes)
        out[ramp] += size * np.asarray(frac, dtype=float)
        out[(out.index >= ramp_end) & (out.index < end)] += size
    return out, end


def main() -> None:
    config = Config()
    data = load_gaia()
    split_at = data.origin + pd.Timedelta(days=config.split_day)
    train_end = split_at
    train_jumps = detect_jumps(data.metrics, config, detect_until=train_end)
    thr = tune_confirm_threshold(train_jumps, [f for f in data.faults if f.start < train_end], config)
    relative = replace(config, memory_jump_mode="relative", memory_jump_ratio=0.3)
    leak_cfg = replace(config, leak_window_minutes=180)
    rng = np.random.default_rng(config.random_seed)

    detectors = {
        "jump_0.5g": lambda m: detect_jumps(m, config),
        "jump_0.5g+threshold": lambda m: [j for j in detect_jumps(m, config) if j.rise_at_confirm >= thr],
        "jump_relative_30pct": lambda m: detect_jumps(m, relative),
        "delta_5m_0.5g": lambda m: detect_delta(m, config),
        "jump+leak_3h": lambda m: detect_jumps(m, config) + detect_leaks(m, leak_cfg),
    }
    rows = []
    for shape, (kind, size, minutes) in SHAPES.items():
        hits = {name: 0 for name in detectors}
        delays = {name: [] for name in detectors}
        total = 0
        for service, part in data.metrics.groupby("service"):
            series = part.set_index("timestamp")["value"].sort_index()
            real = [f for f in data.faults if f.component == service]
            candidates = series.index[(series.index >= split_at + pd.Timedelta(hours=1))]
            chosen: list[pd.Timestamp] = []
            tries = 0
            while len(chosen) < N_PER_SERVICE and tries < 500:
                tries += 1
                t0 = candidates[rng.integers(0, len(candidates))]
                span = pd.Timedelta(minutes=minutes + 60)
                if any(abs(f.start - t0) < span + pd.Timedelta(hours=1) for f in real):
                    continue
                if any(abs(c - t0) < pd.Timedelta(hours=6) for c in chosen):
                    continue
                window = series[(series.index >= t0 - pd.Timedelta(minutes=15)) & (series.index < t0 + span)]
                if len(window) < (minutes + 60) * 1.5:  # needs mostly complete 30 s data
                    continue
                chosen.append(t0)
            for t0 in chosen:
                modified, end = _inject(series, t0, kind, size, minutes)
                lo, hi = t0 - pd.Timedelta(hours=3), end + pd.Timedelta(minutes=30)
                piece = modified[(modified.index >= lo) & (modified.index < hi)]
                frame = pd.DataFrame(
                    {"timestamp": piece.index, "value": piece.to_numpy(), "service": service, "series_id": service}
                )
                total += 1
                for name, detect in detectors.items():
                    found = [
                        j for j in detect(frame)
                        if j.start <= end and j.end >= t0 and j.start >= t0 - pd.Timedelta(minutes=1)
                    ]
                    if found:
                        hits[name] += 1
                        first = min(j.shown_at for j in found)
                        delays[name].append((first - t0).total_seconds() / 60)
        for name in detectors:
            rows.append(
                {
                    "shape": shape,
                    "detector": name,
                    "injected": total,
                    "detected": hits[name],
                    "detection_rate": round(hits[name] / total, 4) if total else 0.0,
                    "confirm_minutes_median": round(float(np.median(delays[name])), 2) if delays[name] else None,
                }
            )
        line = "  ".join(f"{n} {hits[n] / total:.0%}" for n in detectors)
        print(f"{shape:12s} n={total:3d}  {line}")

    out = Path("results") / "robustness_gaia.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"Week-1 confirm threshold {thr / GB:.2f} GB. Wrote {out}")


if __name__ == "__main__":
    main()
