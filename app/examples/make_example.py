"""Write a tiny, fully synthetic export to try the CLI on (no GAIA download).

One day, three services, memory in bytes every 30 s:
  - checkout: a 1.2 GB jump at 09:00 for 8 minutes, with a log line inside it
  - search:   a slow 0.8 GB drift over the day (should NOT fire)
  - payments: metrics stop 14:00-15:30 (a monitoring-gap item)
  - cart:     a slow leak, +1 GB over two hours from 16:00 (a leak item)

    python examples/make_example.py
    python -m product.cli score --alerts examples/alerts.csv --metrics examples/metrics.csv --out queue.csv
"""
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
T0 = pd.Timestamp("2026-03-02", tz="UTC")


def main() -> None:
    rng = np.random.default_rng(7)
    stamps = pd.date_range(T0, T0 + pd.Timedelta(days=1), freq="30s", inclusive="left")
    rows = []
    for service, base in (("checkout", 2.0e9), ("search", 3.0e9), ("payments", 1.5e9), ("cart", 1.0e9)):
        values = base + rng.normal(0, 2e7, len(stamps))
        if service == "checkout":
            hot = (stamps >= T0 + pd.Timedelta(hours=9)) & (stamps < T0 + pd.Timedelta(hours=9, minutes=8))
            values[hot] += 1.2e9
        if service == "search":
            values += np.linspace(0, 0.8e9, len(stamps))
        if service == "cart":
            start, ramp = T0 + pd.Timedelta(hours=16), pd.Timedelta(hours=2)
            frac = ((stamps - start) / ramp).to_numpy(dtype=float).clip(0, 1)
            values += 1.0e9 * frac
        keep = np.ones(len(stamps), bool)
        if service == "payments":
            keep = ~((stamps >= T0 + pd.Timedelta(hours=14)) & (stamps < T0 + pd.Timedelta(hours=15, minutes=30)))
        rows.append(pd.DataFrame({"timestamp": stamps[keep], "value": values[keep], "service": service}))
    pd.concat(rows).to_csv(HERE / "metrics.csv", index=False)

    alerts = [
        ("a1", T0 + pd.Timedelta(hours=3), "search", "info", "cache warm"),
        ("a2", T0 + pd.Timedelta(hours=9, minutes=3), "checkout", "warning", "GC pause 900ms"),
        ("a3", T0 + pd.Timedelta(hours=11), "payments", "error", "card gateway timeout"),
        ("a4", T0 + pd.Timedelta(hours=11, minutes=1), "payments", "error", "card gateway timeout"),
        ("a5", T0 + pd.Timedelta(hours=18), "search", "warning", "slow query 2.1s"),
    ]
    pd.DataFrame(alerts, columns=["alert_id", "timestamp", "source", "severity", "message"]).assign(
        alert_type="log"
    ).to_csv(HERE / "alerts.csv", index=False)
    print(f"Wrote {HERE / 'metrics.csv'} and {HERE / 'alerts.csv'}")


if __name__ == "__main__":
    main()
