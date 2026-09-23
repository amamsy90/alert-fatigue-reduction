"""Synthetic GAIA-like sample data for smoke-testing the pipeline.

Generates two weeks of alerts: injected faults that each produce a burst of
related alerts across a few sources, plus background noise and exact
duplicates. Deterministic via Config.random_seed. This lets the whole M1
pipeline run end-to-end before the real GAIA dataset is downloaded.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SOURCES = ["web-01", "web-02", "db-01", "cache-01", "queue-01"]
NOISE_TYPES = ["cpu_spike", "disk_warn", "latency_tick", "heartbeat_lost"]
SEVERITIES = ["info", "low", "medium", "high", "critical"]


def generate(out_dir: str | Path, seed: int = 42, days: int = 14) -> None:
    rng = np.random.default_rng(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = pd.Timestamp("2026-01-05", tz="UTC")

    alerts: list[dict] = []
    faults: list[dict] = []
    next_id = 0

    def add_alert(ts, source, atype, severity, message):
        nonlocal next_id
        alerts.append(
            {
                "alert_id": f"a{next_id:05d}",
                "timestamp": ts,
                "source": source,
                "alert_type": atype,
                "severity": severity,
                "message": message,
            }
        )
        next_id += 1

    # Background noise: ~120 alerts/day, mostly low severity
    for day in range(days):
        for _ in range(120):
            ts = t0 + pd.Timedelta(days=day, seconds=int(rng.integers(0, 86400)))
            add_alert(
                ts,
                str(rng.choice(SOURCES)),
                str(rng.choice(NOISE_TYPES)),
                str(rng.choice(SEVERITIES, p=[0.4, 0.3, 0.2, 0.08, 0.02])),
                f"routine {rng.choice(NOISE_TYPES)} event",
            )

    # Injected faults: 5 per week; each fault bursts related alerts on 2-3 sources.
    patterns = [
        ("db-01", "cache-01", "db_connection_exhausted"),
        ("web-01", "web-02", "http_5xx_surge"),
        ("queue-01", "worker", "queue_backlog_growth"),
        ("db-01", "web-01", "slow_query_cascade"),
        ("cache-01", "web-02", "cache_miss_storm"),
    ]
    for week in range(2):
        for i, pattern in enumerate(patterns):
            start = t0 + pd.Timedelta(
                days=week * 7 + int(rng.integers(0, 6)),
                seconds=int(rng.integers(0, 72000)),
            )
            duration = pd.Timedelta(minutes=int(rng.integers(30, 240)))
            fid = f"f_w{week}_{i}"
            faults.append(
                {
                    "fault_id": fid,
                    "start": start,
                    "end": start + duration,
                    "component": "*",
                    "description": pattern[2],
                }
            )
            n_alerts = int(rng.integers(8, 20))
            for j in range(n_alerts):
                ts = start + pd.Timedelta(seconds=int(rng.integers(0, int(duration.total_seconds()))))
                src = pattern[j % 2]
                add_alert(
                    ts,
                    src,
                    pattern[2],
                    str(rng.choice(["high", "critical"], p=[0.4, 0.6])),
                    f"{pattern[2]} on {src}",
                )
                # ~30% of fault alerts arrive as exact duplicates
                if rng.random() < 0.3:
                    add_alert(
                        ts + pd.Timedelta(seconds=5),
                        src,
                        pattern[2],
                        "high",
                        f"{pattern[2]} on {src}",
                    )

    pd.DataFrame(alerts).sort_values("timestamp").to_csv(
        out / "alerts.csv", index=False
    )
    pd.DataFrame(faults).to_csv(out / "faults.csv", index=False)
    print(f"Sample data written to {out} ({len(alerts)} alerts, {len(faults)} faults)")
