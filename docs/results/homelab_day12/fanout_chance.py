"""Chance line for A3 fan-out: shift every fault window by a random 1-6 h (seed 42, 20 repeats), same company, same grace rule."""
import sys, json
import numpy as np, pandas as pd
from pathlib import Path
SNAP = Path(sys.argv[1]); OUT = Path(sys.argv[2]); GRACE = pd.Timedelta(minutes=10)
raw = pd.read_csv(SNAP / "data/collected/alerts.csv"); raw["ts"] = pd.to_datetime(raw.timestamp, utc=True, format="mixed")
routed = raw[~raw.alert_type.isin(["Watchdog", "InfoInhibitor"])]
fr = pd.read_csv(SNAP / "data/faults.csv"); c = fr[fr.status == "closed"].copy()
c["start"] = pd.to_datetime(c.start, utc=True); c["end"] = pd.to_datetime(c.end, utc=True)
def count(df):
    ge1 = ge2 = own = 0
    for f in df.itertuples():
        w = routed[(routed.company == f.company) & (routed.ts >= f.start) & (routed.ts <= f.end + GRACE)]
        ge1 += len(w) > 0; ge2 += w.source.nunique() >= 2; own += (w.source == f.component).any()
    return ge1, ge2, own
real = count(c)
rng = np.random.default_rng(42); sims = []
for _ in range(20):
    s = c.copy(); sh = pd.to_timedelta(rng.uniform(1, 6, len(s)), unit="h"); s["start"] += sh; s["end"] += sh
    sims.append(count(s))
sims = np.array(sims)
res = {"real": dict(zip(["ge1", "ge2_services", "own_service"], map(int, real))),
       "shifted_mean": dict(zip(["ge1", "ge2_services", "own_service"], [round(float(x), 1) for x in sims.mean(0)])),
       "shifted_min_max": dict(zip(["ge1", "ge2_services", "own_service"], [[int(a), int(b)] for a, b in zip(sims.min(0), sims.max(0))])), "faults": len(c), "repeats": 20, "seed": 42}
(OUT / "fanout_chance.json").write_text(json.dumps(res, indent=2) + "\n"); print(res)
