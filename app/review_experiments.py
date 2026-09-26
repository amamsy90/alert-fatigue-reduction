"""Stress tests for the headline claims. Run: PYTHONPATH=. python review_experiments.py"""
from __future__ import annotations

import pickle
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import Config
from src.gaia_loader import load_run_folder
from src.ingestion import time_split
from src.memory_spikes import Spike, _episodes, detect_jumps, load_metric_folder, score_spikes

CACHE = Path("results/.review_cache.pkl")  # *.pkl is git-ignored
cfg = Config()
if CACHE.exists():
    alerts, faults, metrics = pickle.loads(CACHE.read_bytes())
else:
    alerts, faults = load_run_folder("data/gaia_raw/run/run")
    metrics = load_metric_folder("data/gaia_metrics")
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_bytes(pickle.dumps((alerts, faults, metrics)))

origin = min(a.timestamp for a in alerts)
split_at = origin + pd.Timedelta(days=cfg.split_day)
_, test_faults = time_split(faults, cfg.split_day)
last = max(a.timestamp for a in alerts)
n_days = (last.normalize() - split_at.normalize()).days + 1
print(f"test faults {len(test_faults)}, days {n_days}, faults/day {len(test_faults)/n_days:.1f}")

fb = defaultdict(list)
for f in test_faults:
    fb[f.component].append(f)


def hits(j):
    return [f for f in fb.get(j.service, []) if j.start <= f.end and j.end >= f.start]


def score(shown, name):
    caught, false = set(), 0
    for j in shown:
        h = hits(j)
        caught.update(f.fault_id for f in h)
        false += not h
    r = len(caught) / len(test_faults)
    print(f"  {name:48s} caught {r:6.1%}  items/day {len(shown)/n_days:5.1f}  false/day {false/n_days:4.1f}")
    return r


# ---- series helpers -------------------------------------------------------
series = {
    sid: part.sort_values("timestamp").set_index("timestamp")["value"]
    for sid, part in metrics.groupby("series_id")
}


def jumps_with_confirm(k_bytes=cfg.memory_jump_bytes):
    """Jump episodes plus causal info: confirmation time and rise at confirmation."""
    out = []
    for sid, s in series.items():
        base = s.rolling("15min", closed="left").median()
        rise = s - base
        hot = rise[(rise > k_bytes) & (rise.index >= split_at)]
        for ep in _episodes(hot, sid, sid, cfg):
            pts = hot[(hot.index >= ep.start) & (hot.index <= ep.end)]
            ep.confirm = pts.index[1]
            ep.rise_at_confirm = float(pts.iloc[:2].max())
            out.append(ep)
    return out


J = jumps_with_confirm()
print(f"\nJumps in test: {len(J)}")

# ---- 1. hindsight vs online ranking ---------------------------------------
print("\n[1] Daily top-K by full-episode peak (current) vs causal online queue, K=20")
by_day = defaultdict(list)
for j in J:
    by_day[j.start.normalize()].append(j)
cur = [j for d in by_day.values() for j in sorted(d, key=lambda x: -x.peak_rise)[:20]]
score(cur, "current: top-20/day by peak (hindsight)")
fifo = [j for d in by_day.values() for j in sorted(d, key=lambda x: x.confirm)[:20]]
score(fifo, "online: first 20 confirmed each day")
score(J, "no cap (all jumps)")
# online with a rise-at-confirmation threshold tuned on week 1 to ~K/day
for thr in (0.6e9, 0.7e9, 0.8e9, 0.9e9):
    sel = [j for j in J if j.rise_at_confirm >= thr]
    d2 = defaultdict(list)
    for j in sel:
        d2[j.start.normalize()].append(j)
    sel = [j for d in d2.values() for j in sorted(d, key=lambda x: x.confirm)[:20]]
    score(sel, f"online: rise@confirm >= {thr/1e9:.1f} GB, first 20/day")

# ---- 2. latency ------------------------------------------------------------
print("\n[2] Real time-to-confirm (confirmation point - fault start) for caught faults")
lat = []
for j in J:
    for f in hits(j):
        lat.append((j.confirm - f.start).total_seconds() / 60)
lat = np.array(lat)
print(f"  median {np.median(lat):.2f} min, p90 {np.percentile(lat,90):.2f}, mean {lat.mean():.2f}")

# ---- 3. chance overlap -----------------------------------------------------
print("\n[3] Chance overlap: shift each service's jumps by a random offset (1-6 h)")
rng = np.random.default_rng(0)
rates = []
for _ in range(20):
    shifted = []
    for sid in series:
        off = pd.Timedelta(minutes=int(rng.integers(60, 360)))
        for j in J:
            if j.service == sid:
                shifted.append(Spike(sid, sid, j.start + off, j.end + off, j.peak_rise))
    caught = {f.fault_id for j in shifted for f in hits(j)}
    rates.append(len(caught) / len(test_faults))
print(f"  chance detection {np.mean(rates):.1%} (range {min(rates):.1%}-{max(rates):.1%}) vs real 86.1%")

# ---- 4. fair metric baselines ----------------------------------------------
print("\n[4] Fair metric baselines (a normal Prometheus-style rule), K=20, online first-come")


def static_rule(q):
    out = []
    for sid, s in series.items():
        thr = s[s.index < split_at].quantile(q)
        hot = s[(s > thr) & (s.index >= split_at)] - thr
        for ep in _episodes(hot, sid, sid, cfg):
            out.append(ep)
    return out


def cap_first(eps, k=20):
    d = defaultdict(list)
    for e in eps:
        d[e.start.normalize()].append(e)
    return [e for v in d.values() for e in sorted(v, key=lambda x: x.start)[:k]]


for q in (0.99, 0.999):
    eps = static_rule(q)
    score(eps, f"static: memory > week-1 p{q*100:g} (no cap)")
    score(cap_first(eps), f"static: memory > week-1 p{q*100:g}, first 20/day")


def delta_rule(minutes=5, k=0.5e9):
    out = []
    for sid, s in series.items():
        prev = s.shift(freq=f"{minutes}min").reindex(s.index, method="nearest", tolerance="31s")
        d = (s - prev)
        hot = d[(d > k) & (d.index >= split_at)]
        out += _episodes(hot, sid, sid, cfg)
    return out


eps = delta_rule()
score(eps, "delta(mem[5m]) > 0.5 GB (no cap)")
score(cap_first(eps), "delta(mem[5m]) > 0.5 GB, first 20/day")

# ---- 5. what the misses look like -------------------------------------------
print("\n[5] Missed faults (no jump overlap): rise seen inside the fault window")
caught = {f.fault_id for j in J for f in hits(j)}
miss_rise = []
for f in test_faults:
    if f.fault_id in caught:
        continue
    s = series.get(f.component)
    if s is None:
        continue
    base = s[(s.index >= f.start - pd.Timedelta("15min")) & (s.index < f.start)].median()
    win = s[(s.index >= f.start) & (s.index <= f.end)]
    miss_rise.append((win.max() - base) / 1e9 if len(win) else np.nan)
mr = pd.Series(miss_rise)
print(f"  missed {len(mr)}; no metric points in window: {mr.isna().sum()}")
print(mr.describe().round(3).to_string())

# overlap of concurrent faults on same service
conc = 0
for svc, fl in fb.items():
    fl = sorted(fl, key=lambda f: f.start)
    for a, b in zip(fl, fl[1:]):
        conc += b.start <= a.end
print(f"\n  back-to-back overlapping faults on the same service: {conc}")

# ---- 6. a smaller budget question: how many items per caught fault ---------------
print("\n[6] Budget pressure: faults/day vs K")
fpd = pd.Series([f.start.normalize() for f in test_faults]).value_counts()
print(f"  faults per day: median {fpd.median():.0f}, max {fpd.max()}, days with >20 faults: {(fpd>20).sum()}/{len(fpd)}")
