# Alert Fatigue Reduction in Cloud Microservice Operations

**Final report**  
**Author:** [Your name]  
**Course / Supervisor:** [Course name / supervisor]  
**Date:** [Date]

This report is what to hand in for the technical mark. Numbers come from scripts under `app/` and are checked by `python run_validate.py`. Detail of every file is in `VALIDATION.md`.

---

## 1. Problem

On a cloud microservice platform, many small services (login, database, cache, web) raise thousands of alerts a day. The person on duty can only check a few. Real faults are missed because attention ran out, not because the monitor was silent. Users of the platform pay that delay.

Today’s usual rule — fixed severity plus deleting exact copies — does not read the alert, does not treat a memory jump as one incident, and does not change when five slots are free or fifty.

**Goal.** Under a fixed daily budget K, catch more injected faults than that rule. The mark is the win plus a walk-forward check, not 95% recall.

**Sector.** Cloud microservice operations only. Not banks or hospitals as separate industries.

---

## 2. Design

One daily list of at most K items:

1. The biggest **memory jumps** take the first slots.
2. Leftover slots go to alerts: **escalate**, then **ask** (only if the alert sits inside a jump).
3. The rest are **hidden**.

A **jump** is memory on one service rising more than **0.5 GB** above the median of the previous **15 minutes**, for at least two points no more than two minutes apart. One jump is one incident. Logs from that service during the jump belong to it.

A missed fault costs **10×** one extra alert. The escalate threshold is the week-1 score that minimizes that cost.

Full design: `design_specification.md`.

---

## 3. Implementation

| Layer | What it is |
|---|---|
| `app/src/` | Library: load GAIA, features, classifier, jump detector, policy, metrics |
| `app/run_*.py` | One script per experiment; writes `app/results/` |
| `app/tests/` | 40 automated tests |
| `app/product/` | Local queue for demo (not used in the scores) |

Clocks: log lines are China time, converted to UTC so they line up with memory files (`src/gaia_loader.py`). That fix changed jump overlap from about 2.5% to the figures below.

There is no text-clustering of incidents. The unused `Incident` group type was removed so the code matches the design.

---

## 4. Training

**Data.** GAIA MicroSS (CloudWise), July–August 2021. 34,863 alerts, 1,367 injected memory faults. Every fault is a short program that holds extra memory on one service, so the correct answer is known.

**Train window.** First 7 days: 3,630 alerts, 141 faults.

| What is learned | How |
|---|---|
| Jump size 0.5 GB | Chosen on week 1 only (`run_memory.py --sweep`). Week 2 is not used. |
| Classifier | Logistic regression on week-1 alerts. Miss = 10× a false alarm. |
| Cost threshold | Lowest week-1 score at which escalating still reduces cost. |

The jump **rule** (rise over own recent median) has no fitted mean. That is why a service whose normal memory level drifts is not flagged for days.

---

## 5. Testing

**Test window.** The next 55 days: 31,233 alerts, 1,226 faults. Days are never shuffled.

A fault is **caught** if a shown jump on its service overlaps the injection window, **or** a surfaced alert carries its fault id.

**Main result** (`run_queue_eval.py`, K = 20 items a day):

| System | Faults caught | Items / day | False / day |
|---|---|---|---|
| Severity + exact duplicates | **0%** | 0.8 | 0.8 |
| Learned alerts only | 12.9% | 5.8 | 0.6 |
| Shared queue, jumps first | **72.8%** | 17.8 | 1.3 |

Same queue at K = 5 / 10 / 50: 19.2% / 38.8% / 86.0%. Baseline stays 0% at every budget.

![Faults caught versus daily budget K](figures/detection_vs_k.svg)

Memory jumps with no budget cap: **86.1%** of test faults overlap a jump (1,056 / 1,226). The first rule (week-1 mean + 3σ) reached 53.8%.

The baseline is 0% because log lines inside a GAIA memory fault are **warnings**, not errors, and most faults leave **no log line at all**.

---

## 6. Validation

The one-shot 72.8% could have been a lucky later period. So the classifier was **retrained** before each later week and that week was scored once. Jump size stayed 0.5 GB.

`run_walkforward.py` — 8 folds, K = 20. Interval from `run_stats.py`.

| Fold | Test week | Faults | Queue | Baseline | Items / day |
|---|---|---|---|---|---|
| 1 | 8–15 Jul | 161 | 84.5% | 0% | 18.1 |
| 2 | 15–22 Jul | 134 | 79.8% | 0% | 17.0 |
| 3 | 22–29 Jul | 162 | 77.8% | 0% | 17.5 |
| 4 | 29 Jul–5 Aug | 163 | 68.7% | 0% | 15.3 |
| 5 | 5–12 Aug | 162 | 71.6% | 0% | 16.1 |
| 6 | 12–19 Aug | 139 | 78.4% | 0% | 17.4 |
| 7 | 19–26 Aug | 173 | **52.6%** | 0% | **11.8** |
| 8 | 26–31 Aug | 129 | 84.5% | 0% | 18.7 |
| **Mean** | | | **74.7%** (95% CI **67.3–80.8%**) | **0%** | |

![Walk-forward detection by later week](figures/walkforward.svg)

The queue beats the baseline in **8/8** later weeks (p = 0.0039 if each week is a coin flip). The mean sits next to 72.8%, so the one-shot test was not a lucky fortnight.

Fold 7 is weaker (**52.6%**). That week showed **11.8 items a day**, so the budget of 20 was **not** full. Other later weeks average **17.2 items a day**. The miss is fewer qualifying jumps that week, not a queue that ran out of slots. The method is not equally strong every week; that week must be reported.

`python run_validate.py` checks that these headlines still match the csv files.

---

## 7. What “success” means on this data

Early write-ups listed 95% recall and 95% detection at K = 20. Those numbers assume every fault leaves a log alert. On GAIA MicroSS that is false. `run_stats.py` counts the signals on the 1,226 test faults:

| Signal | Count | Share | What can catch it |
|---|---|---|---|
| Jump, no log | 869 | **70.9%** | Memory rule only |
| Log and jump | 185 | 15.1% | Either path |
| Log, no jump | 34 | 2.8% | Alerts only |
| Neither (silent) | 138 | 11.3% | Nothing in this system |

A log-only classifier is capped near **17.9%**. That is why alerts alone score 12.9% at K=20, and why 95% was the wrong target.

| Early target | Measured | Verdict |
|---|---|---|
| 95% detection at K=20 | **72.8%** vs **0%** baseline | Beats baseline; 95% is not the claim |
| 95% detection | **86.0%** at K=50 | Jump-rule ceiling (86.1% have a jump) |
| Jump overlaps the fault | **86.1%**; one-point blips ignored | Met |
| Works on every service | Jump overlap **81–91%** on all ten | Met |
| Walk-forward not lucky | Mean **74.7%**, CI **67.3–80.8%**, 8/8 wins | Met |
| ≥90% fewer items than raw alerts | ~18 shown vs 31,233 test alerts | Met as a reduction of the pile |
| MTTA 50% below baseline | Baseline MTTA is empty (0 catches) | Report **~2 minutes** to confirm a jump |
| 80% fewer false items than baseline | 1.3 vs 0.8 per day at K=20 | Not met; more faults are caught |

**Success for this project** is: a designed method, a frozen train window, a held-out test, a walk-forward interval, a win over the baseline on faults caught, and the silent-fault ceiling stated.

Eight of ten services almost never write a log during a fault (0–3%). Only `mobservice1` and `mobservice2` do (~85%). Jumps still overlap 81–91% of faults on every service (`run_stats.py` → `stats_by_service.csv`):

| Service | Test faults | With a log | With a jump |
|---|---|---|---|
| dbservice1 | 129 | 0.8% | 91.5% |
| dbservice2 | 148 | 2.0% | 81.1% |
| logservice1 | 126 | 2.4% | 87.3% |
| logservice2 | 108 | 1.8% | 87.0% |
| mobservice1 | 124 | 84.7% | 83.9% |
| mobservice2 | 116 | 87.1% | 87.1% |
| redisservice1 | 111 | 2.7% | 82.0% |
| redisservice2 | 114 | 0.0% | 86.0% |
| webservice1 | 123 | 0.8% | 84.5% |
| webservice2 | 127 | 0.0% | 89.8% |

The strength is the memory rule plus the shared budget, not the text classifier. At K=20 the leftover alert slots add **no extra** caught faults versus jumps alone (both 72.8%).

---

## 8. Limits (say these if asked)

1. **Every GAIA fault is a memory program** (1,367 / 1,367). Say “on this dataset.” On a live platform, CPU or disk faults need the alert path. Do not swap the dataset in the last month: the local files are memory only, and the main finding is that most of these faults leave no log.
2. **About two minutes**, not 0.2 minutes, to confirm a jump (second hot point).
3. **A company cannot use GAIA weights as is.** Shadow mode and Real / Noise marks exist so they can label their own queue and retrain.
4. Scripts `run_m1`, `run_m2`, `run_m4`, `run_stretch` are **alerts only**. Do not quote them as the system result.

---

## 9. How to reproduce

```powershell
cd app
$env:PYTHONPATH = (Get-Location).Path
python -m pytest -q
python run_stats.py
python run_validate.py
```

Needs local GAIA files at `app/data/gaia_raw/run/run` and `app/data/gaia_metrics` (not in git; they are large).

---

## 10. Deliverables

| Item | Where |
|---|---|
| Design | `design_specification.md` |
| Plan | `project_plan.md` |
| Validation record | `VALIDATION.md` |
| This report | `FINAL_REPORT.md` |
| Code + tests | `app/` |
| Result tables | `app/results/` |
| Figures | `figures/detection_vs_k.svg`, `figures/walkforward.svg` |
| Demo (optional) | http://127.0.0.1:8083/ |
