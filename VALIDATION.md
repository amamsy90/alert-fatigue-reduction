# Training, testing, and validation

This is the record a reviewer should use. Every headline number has a script and a file. The product (web app, PagerDuty) is not part of this validation.

**Data:** GAIA MicroSS, July–August 2021. 34,863 alerts, 1,367 injected memory faults.  
**Split:** first 7 days train (3,630 alerts, 141 faults). Later 55 days test (31,233 alerts, 1,226 faults). No shuffle.

## Protocol

1. **Labels** come from injected faults (service + time window), not from alert severity.
2. **Clocks:** log lines are China time, converted to UTC on load (`src/gaia_loader.py`). Memory csv timestamps are UTC.
3. **Jump size (0.5 GB)** is chosen on **week 1 only** (`run_memory.py --sweep`). The test period is not used to pick it.
4. **Classifier** is logistic regression, miss cost = 10× a false alarm. The escalate threshold is the week-1 score that minimizes cost.
5. **Queue:** one daily budget K. Biggest jumps first; leftover slots go to escalated alerts. A fault is caught if a shown jump overlaps it **or** a surfaced alert carries its id.
6. **Honest MTTA:** a jump is confirmed at its second point, about **two minutes**, not the 0.2 minutes the script prints.

## What was trained

| Piece | Trained on | Frozen before test? |
|---|---|---|
| Jump size 0.5 GB | Week-1 fault overlap | Yes |
| Classifier weights | Week-1 alerts + week-1 jumps | Yes (main split) |
| Cost threshold | Week-1 scores | Yes (main split) |
| Jump rule itself | No fit (rise over own 15-minute median) | Yes |

Walk-forward (below) **retrains** the classifier on all days before each later week. The jump size stays 0.5 GB.

## Main test (one split, 1,226 faults)

Script: `python run_queue_eval.py` → `app/results/queue_eval_gaia.csv`

| Queue at K=20 | Faults caught | Items / day | False / day |
|---|---|---|---|
| Severity baseline | 0.0% | 0.8 | 0.8 |
| Learned alerts only | 12.9% | 5.8 | 0.6 |
| Shared list, jumps first | **72.8%** | 17.8 | 1.3 |

Same queue at other budgets: 19.2% (K=5), 38.8% (K=10), 86.0% (K=50).

Memory jumps alone (no budget): `python run_memory.py --rule jump` → `memory_gaia_jump.csv`  
**86.1%** of test faults overlap a jump (1,056 / 1,226); 256 jumps have no fault. Old rule (mean + 3σ): 53.8%.

## Walk-forward (does the result hold later?)

Script: `python run_walkforward.py` → `app/results/walkforward_gaia.csv`

Train on everything before the fold, score the next 7 days, K=20, jumps first.

| Fold | Test week | Faults | Baseline | Queue |
|---|---|---|---|---|
| 1 | 8–15 Jul | 161 | 0% | 84.5% |
| 2 | 15–22 Jul | 134 | 0% | 79.8% |
| 3 | 22–29 Jul | 162 | 0% | 77.8% |
| 4 | 29 Jul–5 Aug | 163 | 0% | 68.7% |
| 5 | 5–12 Aug | 162 | 0% | 71.6% |
| 6 | 12–19 Aug | 139 | 0% | 78.4% |
| 7 | 19–26 Aug | 173 | 0% | 52.6% |
| 8 | 26–31 Aug | 129 | 0% | 84.5% |

**Mean 74.7%** (95% bootstrap CI **67.3–80.8%**; min 52.6%, max 84.5%). Close to the 72.8% one-shot test. Queue > baseline in **8/8** folds (p = 0.0039). Fold 7 is weaker (52.6%, 11.8 items/day vs K=20 — the budget was not full). The method is not equally strong every week. Baseline stays 0% in every fold.

## Why 95% is the wrong target

Script: `python run_stats.py` → `app/results/stats_gaia.csv`, `stats_by_service.csv`

Of 1,226 test faults: **70.9%** have a jump and no log; **15.1%** have both; **2.8%** have a log only; **11.3%** have neither. All 1,367 injections in the folder are `memory_anomalies`. Jump overlap is 81–91% on every service; log rates are ~0–3% except the two mobile services.

## What the older scripts measure

These are **alerts only**. They do not count a silent memory jump as a catch. Do not quote them as the system result.

| Script | File | What it is |
|---|---|---|
| `run_m1.py` | `m1_gaia.csv` | Baseline only |
| `run_m2.py` | `m2_gaia.csv` | Classifier vs baseline (logs) |
| `run_m4.py` | `m4_gaia.csv` | Cost-threshold policy (logs) |
| `run_stretch.py` | `stretch_gaia.csv` | Ask-a-person on logs only |

## Original 95% targets

The proposal listed 95% recall and 95% detection at K=20. Those assumed every fault leaves a log alert. On this data most do not (**70.9%** have a jump and no log). **72.8% at K=20** and **86.1% jump overlap** are the measured figures. At K=50 the queue reaches 86.0%, the jump-rule ceiling. The mark is the win over baseline plus the walk-forward interval, not 95%.

## Reproduce

```powershell
cd app
$env:PYTHONPATH = (Get-Location).Path
python -m pytest -q
python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run --rule jump
python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run --sweep
python run_queue_eval.py
python run_walkforward.py
python run_stats.py
python run_validate.py
```

`run_validate.py` fails if a headline number in the result csv files no longer matches this document.
