"""Synthetic data for smoke tests and the web-app "Run sample" demo.

`generate` writes two weeks of alerts: injected faults that each produce a
burst of related alerts across a few sources, plus background noise and exact
duplicates. Deterministic via Config.random_seed. The alerts-only scripts
(`experiments/legacy_alerts_only/run_m1.py` and the classifier smoke test) use this. It has no metrics.

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


# (day, hour, service, kind, minutes, fault_id). kind "memory" is a byte jump the
# absolute rule can see. kind "multi" moves cpu, latency and error together and
# leaves memory flat, so the single-series jump rule does not fire.
_MULTI_FAULTS = (
    (1, 10, "api", "memory", 20, "f001"),
    (2, 11, "db", "multi", 30, "f002"),
    (3, 15, "api", "multi", 30, "f003"),
    (4, 9, "db", "memory", 20, "f004"),
    (8, 8, "api", "multi", 20, "f006"),
    (8, 12, "api", "multi", 20, "f007"),
    (8, 18, "db", "multi", 20, "f008"),
    (9, 10, "db", "memory", 20, "f009"),
    (10, 16, "api", "multi", 30, "f010"),
    (15, 11, "api", "multi", 20, "f011"),
)
# False memory bumps: larger than the fault jumps, so the cost-tuned confirm
# bar (which sits under the true jumps) still leaves them in the queue. They
# are not faults, and they compete for slots.
_MULTI_FALSE = (
    (5, 2, "db", 10),
    (8, 1, "api", 10),
    (9, 1, "db", 10),
    (9, 2, "db", 10),
    (9, 3, "db", 10),
)


def generate_multivariate(out_dir: str | Path, seed: int = 42, days: int = 14, freq: str = "1min") -> None:
    """Two services, four channels, faults in week 1 and later.

    Channels are memory, cpu, latency and error. The jump rule, in absolute
    mode, sees only the memory jumps. Write ``metrics.csv``, ``alerts.csv``
    and ``faults.csv``.
    """
    rng = np.random.default_rng(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = pd.Timestamp("2026-01-05", tz="UTC")
    stamps = pd.date_range(t0, periods=days * 24 * 60, freq=freq) if freq == "1min" else pd.date_range(
        t0, t0 + pd.Timedelta(days=days), freq=freq, inclusive="left"
    )
    n = len(stamps)
    rows: list[pd.DataFrame] = []
    for service, memory_base, cpu_base in (("api", 2.0e9, 0.22), ("db", 1.6e9, 0.31)):
        phase = 0.4 if service == "api" else 1.1
        clock = np.linspace(0, 8 * np.pi, n)
        memory = memory_base + 5e6 * np.sin(clock + phase) + rng.normal(0, 1e6, n)
        cpu = cpu_base + 0.02 * np.sin(clock) + rng.normal(0, 0.005, n)
        latency = 18 + 2 * np.sin(clock + phase) + rng.normal(0, 0.4, n)
        error = np.clip(0.01 + 0.002 * np.sin(clock) + rng.normal(0, 0.001, n), 0, None)
        for day, hour, _svc, _minutes in (
            (day, hour, svc, minutes) for day, hour, svc, minutes in _MULTI_FALSE if svc == service and day < days
        ):
            start = t0 + pd.Timedelta(days=day, hours=hour)
            end = start + pd.Timedelta(minutes=_minutes)
            hot = (stamps >= start) & (stamps < end)
            memory[hot] += 2.0e9
        for day, hour, svc, kind, minutes, _fid in _MULTI_FAULTS:
            if svc != service or day >= days:
                continue
            start = t0 + pd.Timedelta(days=day, hours=hour)
            end = start + pd.Timedelta(minutes=minutes)
            hot = (stamps >= start) & (stamps < end)
            if kind == "memory":
                memory[hot] += 1.5e9
            else:
                cpu[hot] = 0.95
                latency[hot] = 220
                error[hot] = 0.45
        for metric, values in (("memory", memory), ("cpu", cpu), ("latency", latency), ("error", error)):
            rows.append(
                pd.DataFrame(
                    {
                        "timestamp": stamps,
                        "value": values,
                        "service": service,
                        "metric": metric,
                        "series_id": f"{service}:{metric}",
                    }
                )
            )
    pd.concat(rows, ignore_index=True).to_csv(out / "metrics.csv", index=False)

    alert_rows = []
    fault_rows = []
    number = 0
    for day, hour, service, kind, minutes, fid in _MULTI_FAULTS:
        if day >= days:
            continue
        start = t0 + pd.Timedelta(days=day, hours=hour)
        end = start + pd.Timedelta(minutes=minutes)
        fault_rows.append(
            {
                "fault_id": fid,
                "start": start,
                "end": end,
                "component": service,
                "description": kind,
            }
        )
        alert_rows.append(
            {
                "alert_id": f"a{number:04d}",
                "timestamp": start + pd.Timedelta(minutes=1),
                "source": service,
                "alert_type": "log",
                "severity": "error",
                "message": f"{kind} on {service}",
                "fault_id": fid,
            }
        )
        number += 1
    for day in range(days):
        for service in ("api", "db"):
            alert_rows.append(
                {
                    "alert_id": f"a{number:04d}",
                    "timestamp": t0 + pd.Timedelta(days=day, hours=3, minutes=15 if service == "db" else 0),
                    "source": service,
                    "alert_type": "log",
                    "severity": "info",
                    "message": "heartbeat",
                    "fault_id": "",
                }
            )
            number += 1
    pd.DataFrame(alert_rows).to_csv(out / "alerts.csv", index=False)
    pd.DataFrame(fault_rows).to_csv(out / "faults.csv", index=False)


def load_multivariate(folder: str | Path) -> tuple[pd.DataFrame, list, list]:
    """Read a folder written by ``generate_multivariate``. Fault ids on alerts are kept."""
    from .ingestion import load_alerts, load_faults

    root = Path(folder)
    metrics = pd.read_csv(root / "metrics.csv")
    metrics["timestamp"] = pd.to_datetime(metrics["timestamp"], utc=True)
    alerts = load_alerts(root / "alerts.csv")
    raw_alerts = pd.read_csv(root / "alerts.csv")
    if "fault_id" in raw_alerts.columns:
        labelled = {
            str(alert_id): fault_id
            for alert_id, fault_id in zip(raw_alerts["alert_id"], raw_alerts["fault_id"])
            if pd.notna(fault_id) and str(fault_id) not in ("", "nan")
        }
        for alert in alerts:
            if alert.alert_id in labelled:
                alert.fault_id = labelled[alert.alert_id]
    faults = load_faults(root / "faults.csv")
    return metrics, alerts, faults
