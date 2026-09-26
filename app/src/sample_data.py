"""Synthetic data for smoke tests and the web-app "Run sample" demo.

`generate` writes two weeks of alerts: injected faults that each produce a
burst of related alerts across a few sources, plus background noise and exact
duplicates. Deterministic via Config.random_seed. The alerts-only scripts
(`run_m1.py` and the classifier smoke test) use this. It has no metrics.

`generate_demo_day` writes one short day the product queue can show in a
click: a memory jump, a monitoring gap, a slow leak, and a few log lines.
Sizes match the built-in absolute rules (0.5 GB jump, 10-minute gap, 0.5 GB
leak over 3 hours). Fewer than 10 alerts, so the demo stays rules-only and
does not fit a confirmation threshold on the same day.
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


def generate_demo_day(out_dir: str | Path) -> None:
    """One synthetic on-call day with metrics the built-in rules can see.

    Memory is in bytes, sampled every 30 s (same cadence as GAIA):

    * checkout — flat, then +1.2 GB for 8 minutes at 09:00 (a jump; two log
      lines fall inside that window).
    * payments — silent from 14:00 to 15:30 (a monitoring gap).
    * cart — flat until 12:00, then +1 GB over two hours and held (a slow leak;
      the 15-minute jump rule does not fire, the 3-hour leak rule does).
    * search — flat. Its log lines are noise, not a metric fault.

    The jump is well above the 0.5 GB absolute rule. The ramp rises about
    0.125 GB per 15 minutes, under that rule, which is why it is a leak.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = pd.Timestamp("2026-03-02", tz="UTC")
    stamps = pd.date_range(t0, t0 + pd.Timedelta(days=1), freq="30s", inclusive="left")
    wobble = 5e6 * np.sin(np.linspace(0, 40, len(stamps)))
    rows = []

    checkout = np.full(len(stamps), 2.0e9) + wobble
    hot = (stamps >= t0 + pd.Timedelta(hours=9)) & (stamps < t0 + pd.Timedelta(hours=9, minutes=8))
    checkout[hot] += 1.2e9
    rows.append(pd.DataFrame({"timestamp": stamps, "value": checkout, "service": "checkout"}))

    payments = np.full(len(stamps), 1.5e9) + wobble
    silent = (stamps >= t0 + pd.Timedelta(hours=14)) & (stamps < t0 + pd.Timedelta(hours=15, minutes=30))
    rows.append(pd.DataFrame({
        "timestamp": stamps[~silent], "value": payments[~silent], "service": "payments",
    }))

    cart = np.full(len(stamps), 1.0e9) + wobble
    ramp = pd.Timedelta(hours=2)
    frac = np.clip(((stamps - (t0 + pd.Timedelta(hours=12))) / ramp).to_numpy(dtype=float), 0, 1)
    cart = cart + 1.0e9 * frac
    rows.append(pd.DataFrame({"timestamp": stamps, "value": cart, "service": "cart"}))

    rows.append(pd.DataFrame({
        "timestamp": stamps, "value": np.full(len(stamps), 3.0e9) + wobble, "service": "search",
    }))
    pd.concat(rows, ignore_index=True).to_csv(out / "metrics.csv", index=False)

    alerts = [
        ("a1", t0 + pd.Timedelta(hours=3), "search", "info", "routine cache refresh"),
        ("a2", t0 + pd.Timedelta(hours=9, minutes=2), "checkout", "warning", "GC pause during memory jump"),
        ("a3", t0 + pd.Timedelta(hours=9, minutes=4), "checkout", "error", "container memory high"),
        ("a4", t0 + pd.Timedelta(hours=11), "payments", "error", "card gateway timeout"),
        ("a5", t0 + pd.Timedelta(hours=11, minutes=5), "payments", "info", "heartbeat ok"),
        ("a6", t0 + pd.Timedelta(hours=18, minutes=40), "search", "warning", "slow query 2.1s"),
    ]
    pd.DataFrame(
        [
            {
                "alert_id": aid,
                "timestamp": ts,
                "source": source,
                "alert_type": "log",
                "severity": severity,
                "message": message,
            }
            for aid, ts, source, severity, message in alerts
        ]
    ).to_csv(out / "alerts.csv", index=False)
    pd.DataFrame(
        [
            {
                "fault_id": "f_checkout_jump",
                "start": t0 + pd.Timedelta(hours=9),
                "end": t0 + pd.Timedelta(hours=9, minutes=8),
                "component": "checkout",
                "description": "synthetic 1.2 GB memory jump",
            }
        ]
    ).to_csv(out / "faults.csv", index=False)
