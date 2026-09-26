# Training, testing, and validation

This is the record a reviewer should use. Every headline number has a script and a file. The web app and connectors are not part of this validation.

**Data:** GAIA MicroSS, July–August 2021. 34,863 log lines, 1,367 injected faults (all `memory_anomalies`, 1 GB for 600 s).
**Split:** first 7 days train. Later 55 days test (**1,223 faults**). One split time for alerts, faults and metrics. No shuffle.

## Protocol

1. **Labels** come from injected faults (service + time window), not from alert severity.
2. **Clocks:** log lines are China time, converted to UTC on load. Metric timestamps are UTC.
3. **Causal queue** (`src/queue.py`): an item is shown when a live system could know it — a jump or leak at its confirming point, a gap 10 minutes after the last point, an alert at its timestamp. At most K a day, first come first served. Alerts may use at most 25% of K. No item is chosen using a later point, a jump's end, or its final peak.
4. **Learned on the training window only:** jump size 0.5 GB (`run_memory.py --sweep`), jump confirmation threshold, classifier and its escalation threshold, static-rule p99.
5. **Caught:** a shown jump or leak on the fault's service overlaps the fault window, or a shown alert carries its fault id. Gap items never count as catches.
6. **Baselines** are rules a team would already run, scored with the same queue: log severity + exact duplicates; `memory > week-1 p99`; `delta(mem[5m]) > 0.5 GB`.

## Main test

Script: `python run_queue_eval.py` → `app/results/queue_eval_gaia.csv`

| Queue at K=20 | Caught | Items / day | False / day | Precision |
|---|---|---|---|---|
| logs_severity | 0.0% | 0.8 | 0.8 | 0% |
| static_p99 | 57.2% | 16.0 | 3.0 | 81% |
| delta_5m (fair baseline) | 68.9% | 17.4 | 2.1 | 88% |
| jumps_online | 67.8% | 17.5 | 2.4 | 86% |
| **jumps_tuned** (detector alone) | **72.6%** | 17.1 | **1.0** | 94% |
| **proposed** (jumps + leaks + gaps + alerts) | **70.2%** | 17.6 | 1.6 | 91% |
| jumps_hindsight (old headline, not causal) | 72.9% | 17.5 | 1.3 | 93% |
| p3_ranker | 70.2% | 17.6 | 1.6 | 91% |
| p3_grouped (2 min) | 71.5% | 17.5 | 1.6 | 91% |
| p3_carryover (≤ 10 slots) | 74.3% | 18.6 | 1.6 | 91% |
| delta_5m_carryover | 72.5% | 18.5 | 2.4 | 87% |

Oracle ceiling at K=20: **84.9%** (22 faults a day > 20 slots). Proposed at K=5 / 10 / 50: 19.5% / 38.6% / 85.8%.

**Misses** (`results/miss_causes_gaia.csv`, proposed, K=20): 166 no metric data, 191 budget full, 4 no jump, 3 below threshold.

## Walk-forward

Script: `python run_walkforward.py` → `app/results/walkforward_gaia.csv`

Everything learned is re-learned before each later week; the week is scored once, K=20.

| Fold | Test week | Faults | With metric data | delta_5m | jumps_tuned | proposed | p3_carryover |
|---|---|---|---|---|---|---|---|
| 1 | 8–15 Jul | 161 | 100% | 77.6% | 84.5% | 82.0% | 84.5% |
| 2 | 15–22 Jul | 134 | 91% | 73.9% | 81.3% | 77.6% | 84.3% |
| 3 | 22–29 Jul | 162 | 94% | 72.2% | 77.2% | 71.0% | 74.7% |
| 4 | 29 Jul–5 Aug | 163 | 82% | 67.5% | 69.3% | 67.5% | 71.8% |
| 5 | 5–12 Aug | 162 | 84% | 69.1% | 71.6% | 71.0% | 77.2% |
| 6 | 12–19 Aug | 139 | 92% | 75.5% | 79.1% | 77.0% | 81.3% |
| 7 | 19–26 Aug | 173 | 61% | 51.4% | 52.0% | 52.0% | 57.8% |
| 8 | 26–31 Aug | 129 | 91% | 78.3% | 83.7% | 82.2% | 83.0% |

Means: delta_5m **70.7%**; jumps_tuned **74.8%** (8/8 wins, p = 0.0039); proposed **72.5%** (95% CI 65.7–78.3%; 6 wins, 1 loss, 1 tie, p = 0.0625); p3_carryover **76.8%** vs delta_5m_carryover 74.3% (7/7 non-tied, p = 0.0078). Fold 7 is a metric outage on all services (20 Aug 13:18 UTC + 2 d 12 h).

## Signals and checks

Script: `python run_stats.py` → `app/results/stats_gaia.csv`, `stats_by_service.csv`

- Test faults: 71.0% jump only, 15.1% log and jump, 2.8% log only, 10.8% no metric data, 0.3% silent with data.
- Jump overlap where metric data exists: **99.6%** (1,053 / 1,057).
- Chance overlap (jumps shifted 1–6 h): **2.6%**.
- Fault start → jump confirmed: median **0.72 min**, p90 0.93 min.
- Faults starting inside a monitoring gap: 170; 23 gap items in the test period.
- False jumps starting within 10 min of a fault on another service: 24.6% (random times: 27.2%) — no sign of mislabelled services.

## Robustness replay

Script: `python run_robustness.py` → `app/results/robustness_gaia.csv`. 120 synthetic faults per shape added to the real series, no budget.

| Shape | Jump 0.5 GB | + learned threshold | Relative +30% | Rate rule | Jump + leak |
|---|---|---|---|---|---|
| Step 0.25 GB | 1% | 0% | 8% | 1% | 1% |
| Step 0.5 GB | 99% | 0% | 58% | 99% | 99% |
| Step 1 GB | 100% | 98% | 95% | 100% | 100% |
| Step 2 GB | 100% | 100% | 100% | 100% | 100% |
| Spike 1 GB, 1 min | 98% | 98% | 92% | 100% | 98% |
| Ramp 1 GB / 30 min | 1% | 1% | 6% | 0% | 99% |
| Leak 1 GB / 3 h | 12% | 3% | 6% | 8% | 73% |
| Control (none) | 6% | 3% | 6% | 6% | 6% |

## Older scripts (alerts only)

`run_m1.py`, `run_m2.py`, `run_m4.py`, `run_stretch.py`, `run_memory_clf.py` score log alerts only and do not count a jump as a catch. Do not quote them as the system result.

## Reproduce

```powershell
cd app
$env:PYTHONPATH = (Get-Location).Path
python -m pytest -q
python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run --sweep
python run_queue_eval.py
python run_walkforward.py
python run_stats.py
python run_robustness.py
python make_figures.py
python run_validate.py
```

`run_validate.py` fails if a headline number in the result files no longer matches this document.
