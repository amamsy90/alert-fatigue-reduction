"""A non-IT example: an online shop's orders per 5 minutes in three regions, 21 days.

Real problems (incidents.csv) are outages where orders DROP (payment provider
down, app release bug) and one traffic surge (a promotion gone viral). The
events file is the shop's notification stream: customer-support tickets and
push-notification campaigns, most of them routine.

It shows the tool is not tied to microservices: any metric + any event stream
+ a list of past problems.

    python examples/make_business_example.py
    # then in the web app: Evaluate my data -> upload the three files in examples/shop/
    python -m product.cli evaluate --metrics examples/shop/orders.csv \\
        --incidents examples/shop/incidents.csv --events examples/shop/notifications.csv
"""
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "shop"
T0 = pd.Timestamp("2026-02-01", tz="UTC")


def main() -> None:
    rng = np.random.default_rng(11)
    stamps = pd.date_range(T0, T0 + pd.Timedelta(days=21), freq="5min", inclusive="left")
    hours = (stamps.hour + stamps.minute / 60).to_numpy()
    daily = 0.35 + 0.65 * np.clip(np.sin((hours - 6) / 24 * 2 * np.pi), 0, None)  # quiet at night
    incidents, rows = [], []
    for region, scale in (("eu", 120.0), ("us", 180.0), ("asia", 90.0)):
        rate = scale * daily * (1 + 0.1 * (stamps.dayofweek.to_numpy() >= 5))
        orders = rng.poisson(rate).astype(float)
        for day in rng.choice(np.arange(2, 21), size=4, replace=False):
            start = T0 + pd.Timedelta(days=int(day), hours=int(rng.integers(9, 20)), minutes=int(rng.integers(0, 12)) * 5)
            length = pd.Timedelta(minutes=int(rng.integers(3, 10)) * 5)
            mask = (stamps >= start) & (stamps < start + length)
            orders[mask] = rng.poisson(rate[mask] * 0.15)
            incidents.append((start, start + length, region, "orders dropped (checkout failing)"))
        if region == "us":
            start = T0 + pd.Timedelta(days=15, hours=19)
            mask = (stamps >= start) & (stamps < start + pd.Timedelta(minutes=40))
            orders[mask] = rng.poisson(rate[mask] * 3.0)
            incidents.append((start, start + pd.Timedelta(minutes=40), region, "promotion surge, stock ran out"))
        rows.append(pd.DataFrame({"time": stamps, "orders": orders, "region": region}))
    OUT.mkdir(exist_ok=True)
    pd.concat(rows).to_csv(OUT / "orders.csv", index=False)
    pd.DataFrame(incidents, columns=["start", "end", "region", "what"]).sort_values("start").to_csv(
        OUT / "incidents.csv", index=False)

    events = []
    for i in range(1500):
        t = T0 + pd.Timedelta(minutes=int(rng.integers(0, 21 * 24 * 60)))
        region = str(rng.choice(["eu", "us", "asia"]))
        kind = rng.choice(["campaign sent", "ticket: where is my order", "ticket: refund", "ticket: login"],
                          p=[0.3, 0.3, 0.2, 0.2])
        events.append((t, region, "info" if kind == "campaign sent" else "warning", kind))
    for start, end, region, _ in incidents:  # complaints pile up during real problems
        for _ in range(int(rng.integers(2, 6))):
            t = start + (end - start) * float(rng.uniform(0.3, 1.0))
            events.append((t, region, "error", "ticket: payment failed"))
    pd.DataFrame(events, columns=["time", "region", "level", "text"]).sort_values("time").to_csv(
        OUT / "notifications.csv", index=False)
    print(f"Wrote {OUT}/orders.csv, incidents.csv ({len(incidents)} incidents), notifications.csv ({len(events)} events)")


if __name__ == "__main__":
    main()
