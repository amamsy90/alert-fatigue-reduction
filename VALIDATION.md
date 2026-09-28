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
| **proposed** (jumps + leaks + gaps + alerts) | **69.6%** | 17.7 | 1.8 | 90% |
| jumps_hindsight (old headline, not causal) | 72.9% | 17.5 | 1.3 | 93% |
| p3_ranker | 69.7% | 17.7 | 1.8 | 90% |
| p3_grouped (2 min) | 70.7% | 17.6 | 1.9 | 89% |
| p3_carryover (≤ 10 slots) | 72.6% | 18.6 | 2.1 | 89% |
| delta_5m_carryover | 72.5% | 18.5 | 2.4 | 87% |

Oracle ceiling at K=20: **84.9%** (22 faults a day > 20 slots). Proposed at K=5 / 10 / 50: 19.5% / 38.8% / 85.8%.

**Misses** (`results/miss_causes_gaia.csv`, proposed, K=20): 166 no metric data, 198 budget full, 4 no jump, 4 below threshold.

## Walk-forward

Script: `python run_walkforward.py` → `app/results/walkforward_gaia.csv`

Everything learned is re-learned before each later week; the week is scored once, K=20.

| Fold | Test week | Faults | With metric data | delta_5m | jumps_tuned | proposed | p3_carryover |
|---|---|---|---|---|---|---|---|
| 1 | 8–15 Jul | 161 | 100% | 77.6% | 84.5% | 82.0% | 84.5% |
| 2 | 15–22 Jul | 134 | 91% | 73.9% | 81.3% | 74.6% | 79.8% |
| 3 | 22–29 Jul | 162 | 94% | 72.2% | 77.2% | 71.0% | 73.5% |
| 4 | 29 Jul–5 Aug | 163 | 82% | 67.5% | 69.3% | 67.5% | 69.9% |
| 5 | 5–12 Aug | 162 | 84% | 69.1% | 71.6% | 71.0% | 77.2% |
| 6 | 12–19 Aug | 139 | 92% | 75.5% | 79.1% | 74.1% | 79.9% |
| 7 | 19–26 Aug | 173 | 61% | 51.4% | 52.0% | 50.9% | 54.9% |
| 8 | 26–31 Aug | 129 | 91% | 78.3% | 83.7% | 79.1% | 79.1% |

Means: delta_5m **70.7%**; jumps_tuned **74.8%** (8/8 wins, p = 0.0039); proposed **71.3%** (95% CI 64.5–76.7%; 4 wins, 3 losses, 1 tie, p = 0.5); p3_carryover **74.8%** vs delta_5m_carryover 74.3% (3 of 6 decided weeks, p = 0.66). Numbers after the look-ahead fixes (FINAL_REPORT §5); `run_validate.py` checks them against the report's Checked numbers table. Fold 7 is a metric outage on all services (20 Aug 13:18 UTC + 2 d 12 h).

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

## Other domains (NAB)

Script: `python ../tools/fetch_nab.py && python run_nab.py` → `app/results/nab_eval.csv`, `nab_settings.csv`. Tuned on the first half of each series, K = 2.

| Dataset | Test problems | Your queue | Chance |
|---|---|---|---|
| realAWSCloudwatch | 17 | 70.6% | 57.6% |
| realAdExchange | 6 | 83.3% | 58.0% |
| realKnownCause | 13 | 38.5% | 15.4% |
| realTraffic | 11 | 36.4% | 11.6% |
| realTweets | 11 | 36.4% | 36.0% |

## Second benchmark: other fault types (SMD)

Script: `python ../tools/fetch_smd.py && python run_smd.py` → `app/results/smd_eval.csv`. Server Machine Dataset (OmniAnomaly, KDD 2019): 28 real servers, five weeks of one-minute samples, 38 metrics each (CPU, memory, disk, network, TCP), 196 labelled incidents in the test part with, for each, the metrics that moved. Tuned on the first half of every series, scored on the second half, K items a day **per machine**, same engine as the Evaluate page (22 of 28 machines had incidents to tune on; the rest ran with defaults).

| Budget K per machine | Incidents caught (196) | Metric movements shown (1,536) | Items / day | False / day | Chance (per movement) |
|---|---|---|---|---|---|
| 5 | 60 (30.6%) | 10.1% | 4.2 | 3.5 | 12.4% |
| 10 | 83 (42.3%) | 19.3% | 7.9 | 6.5 | 12.4% |
| 20 | 108 (55.1%) | 33.9% | 13.8 | 11.0 | 12.4% |

*Incident caught* = any metric that moved during the incident was shown while it lasted. *Metric movements* is the stricter count (each flagged metric on its own). "False" items are shown metrics that no label covers; SMD labels only some of the 38 metrics per incident, so correlated movements on unlabelled metrics count as false here. On 11 of 28 machines the detector picked on the tuning half was also the best on the test half; on the others the static rule usually was. This is a harder benchmark than GAIA (many small, mixed-metric incidents, one machine = 38 series) and the numbers are reported as they are.

## Older scripts (alerts only)

The scripts in `app/experiments/legacy_alerts_only/` (`run_m1`, `run_m2`, `run_m4`, `run_stretch`, `run_memory_clf`) score log alerts only and do not count a jump as a catch. Do not quote them as the system result.

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
