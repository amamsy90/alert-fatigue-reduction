# Budgeted Incident Detection for Cloud Microservice On-Call

*(Alert Fatigue Reduction in Cloud Microservice Operations)*

**Final report**
**Author:** Aishah Mohamed Alyammahi
**Course:** Software Engineering and Programming
**Date:** [Date]

Numbers come from scripts under `app/` and are checked by `python run_validate.py`. Every file is listed in `VALIDATION.md`. The review that led to this version is `CODE_REVIEW.md`; the plan is `ROADMAP.md`.

---

## 1. Problem

On a cloud microservice platform, many small services raise thousands of alerts a day, and the person on duty can check only a few. Real faults are missed because attention runs out, not because the monitor was silent.

The usual rule — fixed log severity plus deleting exact copies — reads only logs. On the dataset used here, **82% of injected faults never write a log line** during the fault. They only show up in metrics.

**Goal.** Under a fixed daily budget of K items, catch more injected faults than the rules a team would already run, **in real time** (no item may be chosen using information from later in the day), with fewer false items.

**Sector.** Cloud microservice operations only.

---

## 2. Design

One queue, at most K items a day, **first come, first served**:

1. **Metric jumps.** A point is hot when it is more than **0.5 GB** above the median of the previous **15 minutes** on the same service. Two hot points no more than two minutes apart **confirm** a jump. A jump is shown at its confirmation point, and only if its rise at that point is at least a threshold learned on the training window (0.62 GB on week 1).
2. **Slow leaks.** The same rule with a **3-hour** median and 10 minutes of hot points catches memory that climbs over hours, which the 15-minute rule cannot see. Leaks that overlap a fast jump are dropped.
3. **Monitoring gaps.** A service with no metric point for **10 minutes** is shown as a gap item ("we are blind here"). Gaps that start together on several services are one item.
4. **Log alerts** may use at most **a quarter** of the slots (5 of 20). An alert on a service whose shown jump is still running rides on that jump and uses no slot (one incident, one item).

Nothing in the queue looks at a later point, the end of a jump, or its final peak. A missed fault costs **10×** a false item; both learned thresholds (jump confirmation, alert escalation) minimise that cost on the training window only.

For metrics that are not memory in bytes, a **relative mode** marks a point hot when it is, say, 30% above its recent median (`JUMP_MODE=relative`). Days can start at any hour (`DAY_OFFSET_HOURS`), and every per-day budget uses the same cut.

Full design: `design_specification.md`.

---

## 3. Implementation

| Layer | What it is |
|---|---|
| `app/src/` | Library: GAIA loader, jump / leak / gap detectors, metric baselines, causal queue, jump ranker, alert classifier, policy, statistics |
| `app/src/experiment.py` | One train/test window, every system, one scorer — used by every GAIA results script |
| `app/src/generic.py` | The same evaluation for **any** data: column detection, per-series split, tuning, report |
| `app/run_*.py` | One script per experiment; writes `app/results/` |
| `app/tests/` | 150 automated tests |
| `app/product/` | Web app — queue, **Evaluate my data** page, shadow mode, Real/Noise marks, Alertmanager webhook, Prometheus, PagerDuty / Slack / Opsgenie — and a CLI (`score`, `evaluate`, `tune`) |

Log lines are China time and are converted to UTC so they line up with the metric files (`src/gaia_loader.py`).

**Operating safely.** The product side was hardened after review so that a team can run it next to a real pager: paging is off by default (shadow mode) and Live must be typed in; only live webhook jobs may page, never the sample, GAIA or Evaluate runs; a paging watermark means only items confirmed since the last job are sent, so switching to Live never pages the two weeks of history a job re-scores; every configured channel (PagerDuty, Slack, Opsgenie) gets the same item list; one job runs at a time; jobs and evaluations are pruned (Real / Noise marks are kept separately); uploads are capped; and secrets are stored outside the repository and never shown in full.

---

## 4. Training

**Data.** GAIA MicroSS (CloudWise), July–August 2021: 34,863 log lines, 1,367 injected faults. Every fault is the same program: it holds **1 GB of extra memory for 600 s** on one service, so the correct answer is known.

**Train window.** The first 7 days. **Test window.** The next 55 days: **1,223 faults**. Days are never shuffled; one split time is used for alerts, faults and metrics alike.

| What is learned | How |
|---|---|
| Jump size 0.5 GB | Chosen on week 1 only (`run_memory.py --sweep`; 0.3 and 0.5 GB tie on week-1 detection, 0.5 GB has half the false jumps) |
| Jump confirmation threshold | Rise at confirmation with the lowest week-1 cost → **0.62 GB** |
| Alert classifier | Logistic regression on week-1 alerts; a miss weighs 10× a false alarm |
| Alert escalation threshold | Lowest week-1 cost |
| Jump ranker (Phase 3) | Logistic regression on week-1 jumps, features known at confirmation |
| Static-rule baseline | Week-1 99th percentile per service |

---

## 5. Testing

**Caught** = a shown jump or leak on the fault's service overlaps the injection window, or a shown alert carries the fault's id. Gap items never count as catches.

**Baselines are the rules a team would already have**, all scored with the same causal queue:

| System (K = 20 items a day) | Caught | Share of best possible | Items / day | False / day | Precision |
|---|---|---|---|---|---|
| Log severity + exact duplicates | 0.0% | 0% | 0.8 | 0.8 | 0% |
| Static rule: memory > week-1 p99 | 57.2% | 67% | 16.0 | 3.0 | 81% |
| Rate rule: `delta(mem[5m]) > 0.5 GB` | 68.9% | 81% | 17.4 | 2.1 | 88% |
| Jump rule, first come | 67.8% | 80% | 17.5 | 2.4 | 86% |
| **Jump rule + learned threshold** (detector alone) | **72.6%** | **86%** | 17.1 | **1.0** | **94%** |
| **Full queue** (jumps + leaks + gaps + alerts) | **69.6%** | **82%** | 17.7 | 1.8 | 90% |
| *Old headline: day's jumps ranked by final peak (not real time)* | *72.9%* | *86%* | *17.5* | *1.3* | *93%* |

**Best possible at K = 20 is 84.9%**: GAIA injects 22 faults a day, more than 20 on 34 of 55 days.

At K = 5 / 10 / 50 the full queue catches 19.5% / 38.8% / 85.8% (rate rule: 19.3% / 38.6% / 86.0%). Its advantage is at the realistic budget K = 20, where slots are scarce, and in false items (1.0–1.8 a day vs 2.1).

**Removing the last look-ahead.** A code review found five places where the queue still used information from later in the day, although the text said it did not. The per-day alert filters kept each day's *most severe* (or highest-scoring) K alerts, which needs the whole day; an overlapping jump that arrived *after* a leak was confirmed could still remove that leak; the robust score used the spread of the whole series; and an alert "rode on" a jump using the jump's final end. All five now use only what is known at the moment of the decision: alerts spend the budget first come, first served; a leak is dropped only by a jump already shown; the robust spread uses earlier points only; and an alert rides on a jump only while the jump's last hot point is within its gap (`Spike.open_at`). `tests/test_causality.py` checks each one. The honest cost is small: the full queue drops from 70.2% to 69.6% at K = 20 and shows 1.8 false items a day instead of 1.6. The detector alone is unchanged (72.6%), because none of the five touched it.

**Why the full queue scores below the detector alone on GAIA.** Every GAIA fault is a 1 GB memory fault, so leak items (0.2 a day), gap items (0.4 a day) and log alerts spend slots that jumps would have used. On a platform with leaks, outages, and CPU, disk or code faults, those are the parts that catch them (Section 7). Both rows are reported; the detector comparison is the first claim, the full queue is what the tool runs.

The old ranking needed the whole day in advance. Replacing it with a learned confirmation threshold keeps the score (72.6% vs 72.9%), cuts false items (1.0 vs 1.3 a day), and makes the queue usable live.

**Time to confirm.** Median **0.72 minutes** from fault start to the confirming point (p90 0.93). Metrics are sampled every 30 seconds.

**Chance check.** If each service's jumps are shifted by a random 1–6 hours, only **2.6%** of faults still overlap one.

![Faults caught versus daily budget K](figures/detection_vs_k.svg)

### Where the misses come from

The full queue misses 372 of the 1,223 test faults at K = 20 (`results/miss_causes_gaia.csv`):

| Cause | Faults | Share of misses |
|---|---|---|
| No metric data during the fault (monitoring outage) | 166 | 45% |
| A qualifying jump existed, but the day's 20 slots were used | 198 | 53% |
| Metric data, but no jump overlapped | 4 | 1% |
| A jump overlapped, but below the confirmation threshold | 4 | 1% |

**Only 2% of misses are the detector's fault.** The rest are data outages (which the queue flags as gap items) and a budget smaller than the fault rate.

---

## 6. Validation (walk-forward)

Before each later week, **everything learned is re-learned from the past only** — the classifier, its threshold, the jump ranker and the jump confirmation threshold (0.62–0.75 GB across folds). That week is then scored once. `run_walkforward.py`, K = 20.

| Fold | Test week | Faults | With metric data | Rate rule | Detector alone | Full queue |
|---|---|---|---|---|---|---|
| 1 | 8–15 Jul | 161 | 100% | 77.6% | 84.5% | 82.0% |
| 2 | 15–22 Jul | 134 | 91% | 73.9% | 81.3% | 74.6% |
| 3 | 22–29 Jul | 162 | 94% | 72.2% | 77.2% | 71.0% |
| 4 | 29 Jul–5 Aug | 163 | 82% | 67.5% | 69.3% | 67.5% |
| 5 | 5–12 Aug | 162 | 84% | 69.1% | 71.6% | 71.0% |
| 6 | 12–19 Aug | 139 | 92% | 75.5% | 79.1% | 74.1% |
| 7 | 19–26 Aug | 173 | **61%** | 51.4% | 52.0% | 50.9% |
| 8 | 26–31 Aug | 129 | 91% | 78.3% | 83.7% | 79.1% |
| **Mean** | | | | **70.7%** | **74.8%** | **71.3%** (95% CI 64.5–76.7%) |

- The **detector** (jump rule + learned threshold) beats the rate rule in **8/8** weeks (sign test p = 0.0039).
- The **full queue** does **not** beat the rate rule week by week: 4 wins, 3 losses, 1 tie (p = 0.5). Before the look-ahead fixes in Section 5 it won 6 of 8; part of that margin came from choosing each day's alerts with hindsight. On GAIA its extra item types cost slots; see Section 5.
- The comparison is against a real rule. The log-severity rule scores 0% every week, so a test against it would prove nothing.

**Fold 7** is weak because of a **monitoring outage**: every service's metrics stop from 20 Aug 13:18 UTC for 2 days 12 hours, so only 61% of that week's faults have any metric data. The full queue shows this outage as a gap item within 10 minutes. No metric-based method could catch those faults.

![Walk-forward detection by later week](figures/walkforward.svg)

---

## 7. Robustness: does it only work because every fault is 1 GB?

`run_robustness.py` adds synthetic faults of other shapes to the **real** GAIA memory series (120 per shape, at least an hour from any real fault) and asks whether each detector fires, with no budget:

| Injected shape | Jump rule 0.5 GB | + learned threshold | Relative +30% | Rate rule | Jump + leak rule |
|---|---|---|---|---|---|
| Step +0.25 GB, 10 min | 1% | 0% | 8% | 1% | 1% |
| Step +0.5 GB, 10 min | 99% | **0%** | 58% | 99% | 99% |
| Step +1 GB, 10 min (GAIA shape) | 100% | 98% | 95% | 100% | 100% |
| Step +2 GB, 10 min | 100% | 100% | 100% | 100% | 100% |
| Spike +1 GB, 1 min | 98% | 98% | 92% | 100% | 98% |
| Ramp +1 GB over 30 min | 1% | 1% | 6% | 0% | **99%** |
| Leak +1 GB over 3 h | 12% | 3% | 6% | 8% | **73%** |
| Nothing injected (control) | 6% | 3% | 6% | 6% | 6% |

What this shows:

- **Faults smaller than the jump size are invisible** (0.25 GB). A team must set the size for its own services.
- **The learned threshold over-fits the benchmark**: trained on 1 GB faults, it rejects every 0.5 GB fault. The tool therefore uses **no** confirmation threshold unless it is trained on the team's own incidents.
- **Leaks and ramps defeat both the jump rule and the Prometheus rate rule.** The leak rule catches 99% of 30-minute ramps and 73% of 3-hour leaks, at a cost of 0.3 extra items a day on real GAIA data (none of them near a fault), and no extra hits on the control windows.

---

## 8. Phase 3 queue options

| Option (K = 20) | Caught | Items / day | Walk-forward mean | Verdict |
|---|---|---|---|---|
| Full queue (reference) | 69.6% | 17.7 | 71.3% | — |
| **Learned jump ranker** instead of the rise threshold | 69.7% | 17.7 | 71.8% | No gain: the rise threshold already gives 94% precision, so there is little left to learn from service, time of day or nearby logs |
| **Group** metric items within 2 min on other services into one slot | 70.7% | 17.6 | 72.6% | Small gain; one item may then name two services |
| **Confirm level per series** (learned on week 1; off in the headline) | 69.3% | 17.6 | 70.5% | Mixed: the detector alone keeps 72.6% with fewer false items (0.8 vs 1.0 a day), but the levels reject real faults — 26 misses below threshold instead of 4, and 84.1% vs 85.8% at K = 50. The web app uses it; the published tables use the single gate |
| **Carry over** up to 10 unused slots to the next day | **72.6%** | 18.6 | **74.8%** | Largest gain; the average stays under K. But the same carry-over lifts the rate rule to 72.5% (walk-forward 74.3%), and the full queue then wins only 3 of 6 decided weeks (p = 0.66). Before the look-ahead fixes this row read 74.4% and 8/8 weeks; that margin did not survive |

Carry-over and grouping are off by default (`CARRY_OVER_MAX`, `GROUP_MINUTES`): they change what "20 a day" means for the on-call team, so it is their choice.

---

## 9. Beyond microservices: any metric, any team

The engine is not tied to GAIA. `src/generic.py` takes any **metrics** table (time, value, optional series; long or wide; column names detected), optional **events** (alerts, tickets, notifications) and optional **past problems** (start, end, series). For each series it tunes on the first part and scores the rest once, and every system — static threshold, rate rule, a unit-free robust jump rule, the user's own alerts — is tuned with the same cost and scored through the same causal queue. The detector with the lowest training cost becomes the user's queue. A **chance line** (the same items at random times) is reported so wide labels cannot flatter the result.

This runs from the web app (**Evaluate my data**: upload or pick a dataset → read the report → *Use these settings for my live queue* → *Show this data in the queue*) and from the CLI (`evaluate`, `tune`). The guide for end users is `USE_YOUR_DATA.md`.

Results on data that is not microservices (tuned on the first half of each series, K = 2 items a day; `run_nab.py`, `results/nab_eval.csv`):

| Dataset | Domain | Test problems | Queue caught | Chance | Items / day |
|---|---|---|---|---|---|
| Online shop (synthetic; K = 5, 30% tuning) | e-commerce orders + notifications | 11 | 91% | 1% | 1.3 |
| NAB realAWSCloudwatch | cloud servers | 17 | 71% | 58% | 0.3 |
| NAB realAdExchange | online advertising | 6 | 83% | 58% | 1.4 |
| NAB realKnownCause | machine temperature, taxi demand, CPU | 13 | 38% (static rule 54%) | 15% | 0.04 |
| NAB realTraffic | road traffic sensors | 11 | 36% (rate rule 45%) | 12% | 0.3 |
| NAB realTweets | Twitter mention volume | 11 | 36% | 36% | 1.7 |

On the shop data the monitoring view catches 10 of 11 problems where the notification stream by severity catches 3. On NAB the queue is clearly above chance on traffic and machine data, modestly on cloud servers and ads (NAB's anomaly windows are wide, so chance is high), and **no better than chance on Twitter volume**. The test sets are small (6–17 problems), and on one dataset the detector chosen on the tuning part was not the best on the test part. These are exactly the facts a new user needs before relying on it, which is why the app shows them.

**Other fault types: the Server Machine Dataset (SMD).** GAIA has one fault type. SMD (OmniAnomaly, KDD 2019) has 28 real servers, five weeks of one-minute samples, 38 metrics each — CPU, memory, disk, network, TCP — and 196 labelled test incidents, each with the metrics that moved. Each machine is one dataset: tuned on the first half of every series, scored on the second half, K items a day per machine (`run_smd.py`, `results/smd_eval.csv`):

| Budget K per machine | Incidents caught (196) | Metric movements shown (1,536) | Items / day | False / day | Chance (per movement) |
|---|---|---|---|---|---|
| 5 | 60 (30.6%) | 10.1% | 4.2 | 3.5 | 12.4% |
| 10 | 83 (42.3%) | 19.3% | 7.9 | 6.5 | 12.4% |
| 20 | 108 (55.1%) | 33.9% | 13.8 | 11.0 | 12.4% |

An incident counts once and is caught if any metric that moved was shown while it lasted; the movement column counts every flagged metric on its own. Half of the incidents are found at 20 items a day per machine on real mixed-metric faults, well above the 12.4% chance line, but with low precision: SMD labels only some of the 38 metrics per incident, so a correlated movement on an unlabelled metric counts as false. Where a series had three or more tuning incidents the jump rule learned its own size (Section 4 of `USE_YOUR_DATA.md`). This is a harder benchmark than GAIA and it is reported as it is.

---

## 10. What "success" means on this data

`run_stats.py` splits the 1,223 test faults by the signal they left:

| Signal | Count | Share | What can catch it |
|---|---|---|---|
| Jump, no log | 868 | **71.0%** | Metric rule only |
| Log and jump | 185 | 15.1% | Either path |
| Log, no jump | 34 | 2.8% | Alerts only |
| No log, no metric data (monitoring gap) | 132 | 10.8% | Nothing — shown as a gap item |
| No log, data but no jump | 4 | 0.3% | Nothing |

- **Where metric data exists, the jump rule overlaps 99.6% of faults** (1,053 / 1,057).
- 170 test faults start inside a monitoring gap. At K = 20 the gap items shown cover 159 of them (all 170 at K = 50).
- **False jumps are not mislabelled faults.** 24.6% of the 256 false jumps start within 10 minutes of a fault on another service; random times do so 27.2% of the time. There is no sign that GAIA logged faults under the wrong service.
- A log-only classifier is capped near **17.9%** (the faults that leave any log). That is why the early 95% target was wrong.

| Early target | Measured | Verdict |
|---|---|---|
| 95% detection at K=20 | 69.6–72.6% (ceiling 84.9%) | Not possible at K=20: more faults per day than slots. 82–86% of the ceiling |
| Beat the usual rule | Detector +3.7 points over a rate rule, 8/8 weeks; full queue +0.7 points, 4 of 8 weeks; log severity 0% | Met by the detector, against a fair rule; the full queue ties it |
| Fewer false items | 1.0–1.8 vs 2.1 a day (rate rule) | Met |
| Works on every service | Jump overlap 81–91% on all ten | Met |
| Walk-forward not lucky | Means 71.3–74.8%, CI 64.5–76.7% | Met |
| Real-time | Confirmed in 0.72 min; no look-ahead, checked by `tests/test_causality.py` | Met |
| Works beyond 1 GB faults | 0.5–2 GB steps and spikes ≥ 98%; leaks 73–99% with the leak rule; 0.25 GB not | Partly — stated |

Per service (test period):

| Service | Faults | With a log | With a jump |
|---|---|---|---|
| dbservice1 | 129 | 0.8% | 91.5% |
| dbservice2 | 148 | 2.0% | 81.1% |
| logservice1 | 126 | 2.4% | 87.3% |
| logservice2 | 108 | 1.8% | 87.0% |
| mobservice1 | 124 | 84.7% | 83.9% |
| mobservice2 | 116 | 87.1% | 87.1% |
| redisservice1 | 111 | 2.7% | 82.0% |
| redisservice2 | 114 | 0.0% | 86.0% |
| webservice1 | 122 | 0.8% | 85.2% |
| webservice2 | 125 | 0.0% | 90.4% |

---

## 11. Limits (say these if asked)

1. **Every GAIA fault is the same 1 GB, 600 s memory program.** The robustness replay (Section 7) tests other sizes and shapes; Section 9 tests other domains (NAB, a shop), with small test sets and a chance line. CPU, disk or network faults are now measured on real servers (SMD, Section 9) but not inside microservices.
2. **The win over a plain Prometheus rate rule is modest** (+3.7 points for the detector, 8/8 weeks; +0.7 for the full queue, which ties it week by week). The larger gains are fewer false items, a real-time queue, and leak and gap coverage. Carry-over lifts both rules about equally.
3. **K = 20 is below the fault rate** (22 a day), so no method can exceed 84.9%.
4. **Learned thresholds do not transfer.** One learned on GAIA's 1 GB faults rejects 0.5 GB faults. Rules-only mode works without labels. When a batch has a training window and faults, the queue now also learns a **confirm level per series** on that window only (same cost: 10 × miss + false) and freezes it. Series with too little training signal keep the single global level. This is the metric-side learner. The sklearn classifier is still only for log alerts. The headline table above is the earlier **single** threshold (0.62 GB on week 1); it has not been re-measured with per-series gates.
5. `run_m1`, `run_m2`, `run_m4`, `run_stretch` are alerts-only experiments. Do not quote them as the system result.

**Future work.** Microservice benchmarks with other fault types (for example RCAEval or TrainTicket-based sets). The Evaluate engine now learns a per-series jump size as well as a per-series confirm gate (3+ tuning incidents, kept only where it beats the shared size); the GAIA headline still uses the single 0.5 GB rule, so a 0.25 GB fault is still invisible there.

---

## 12. How to reproduce

```powershell
cd app
$env:PYTHONPATH = (Get-Location).Path
python -m pytest -q
python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run --sweep
python run_queue_eval.py
python run_walkforward.py
python run_stats.py
python run_robustness.py
python ../tools/fetch_nab.py      # needs internet, ~15 MB
python run_nab.py
python ../tools/fetch_smd.py      # needs internet, ~240 MB
python run_smd.py                # 28 machines, about 15 min; resumes if interrupted
python make_figures.py
python run_validate.py
```

Needs the GAIA files at `app/data/gaia_raw/run/run` and `app/data/gaia_metrics` (not in git; they are large). Without GAIA: `python examples/make_example.py`, then `python -m product.cli score --alerts examples/alerts.csv --metrics examples/metrics.csv`.

---

## 13. Deliverables

| Item | Where |
|---|---|
| Design | `design_specification.md` |
| Requirements and traceability | `docs/requirements.md` |
| Architecture diagrams | `docs/architecture.md` |
| Design decisions | `docs/adr/` |
| Threat model | `docs/threat_model.md` |
| Test plan and CI gates | `docs/test_plan.md`, `.github/workflows/ci.yml` |
| Retrospective | `docs/retrospective.md` |
| Change history | `CHANGELOG.md` |
| Plan / roadmap | `project_plan.md`, `ROADMAP.md` |
| Validation record | `VALIDATION.md` |
| Review of the earlier version | `CODE_REVIEW.md` |
| This report | `FINAL_REPORT.md` |
| Code + tests | `app/` |
| Result tables | `app/results/` |
| Figures | `figures/detection_vs_k.svg`, `figures/walkforward.svg` |
| Tool | Web app `docker compose up` (http://127.0.0.1:8080/, **/evaluate** for your own data), CLI `python -m product.cli` |
| End-user guide | `USE_YOUR_DATA.md` |

---

## Checked numbers

`python run_validate.py` (in `app/`) recomputes every value below from `app/results/` and fails if one no longer rounds to what is printed here, if a key is missing, or if a sentence it guards (for example "the detector wins 8/8 weeks") stops being true. Rates are shares of test faults; `false` is false items a day.

| Key | Value | Where in this report |
|---|---|---|
| `queue.K20.logs_severity` | 0.0% | §5 table |
| `queue.K20.static_p99` | 57.2% | §5 table |
| `queue.K20.delta_5m` | 68.9% | §5 table |
| `queue.K20.jumps_online` | 67.8% | §5 table |
| `queue.K20.jumps_tuned` | 72.6% | §5 table |
| `queue.K20.proposed` | 69.6% | §5 table |
| `queue.K20.jumps_hindsight` | 72.9% | §5 table |
| `queue.K50.proposed` | 85.8% | §5 text |
| `false.K20.delta_5m` | 2.1 | §5 table |
| `false.K20.jumps_tuned` | 1.0 | §5 table |
| `false.K20.proposed` | 1.8 | §5 table |
| `ceiling.K20` | 84.9% | §5 |
| `misses.K20.no_metric_data` | 166 | §5 misses |
| `misses.K20.budget_full` | 198 | §5 misses |
| `misses.K20.no_jump` | 4 | §5 misses |
| `misses.K20.below_threshold` | 4 | §5 misses |
| `walkforward.proposed.mean` | 71.3% | §6 |
| `walkforward.proposed.ci_low` | 64.5% | §6 |
| `walkforward.proposed.ci_high` | 76.7% | §6 |
| `walkforward.proposed.wins` | 4 | §6 |
| `walkforward.jumps_tuned.mean` | 74.8% | §6 |
| `walkforward.jumps_tuned.wins` | 8 | §6 |
| `walkforward.delta_5m.mean` | 70.7% | §6 |
| `robustness.step_1g.jump_0.5g` | 100% | §7 |
| `robustness.step_0.25g.jump_0.5g` | 1% | §7 |
| `robustness.ramp_1g.jump+leak_3h` | 99% | §7 |
| `robustness.step_0.5g.jump_0.5g+threshold` | 0% | §7 |
| `queue.K20.p3_ranker` | 69.7% | §8 |
| `queue.K20.p3_grouped` | 70.7% | §8 |
| `queue.K20.p3_per_series` | 69.3% | §8 |
| `queue.K50.p3_per_series` | 84.1% | §8 |
| `false.K20.p3_per_series_jumps` | 0.8 | §8 |
| `queue.K20.p3_carryover` | 72.6% | §8 |
| `queue.K20.delta_5m_carryover` | 72.5% | §8 |
| `walkforward.carryover.mean` | 74.8% | §8 |
| `walkforward.delta_carryover.mean` | 74.3% | §8 |
| `nab.realAWSCloudwatch` | 71% | §9 |
| `nab.realAdExchange` | 83% | §9 |
| `nab.realKnownCause` | 38% | §9 |
| `nab.realTraffic` | 36% | §9 |
| `nab.realTweets` | 36% | §9 |
| `smd.K5.incidents` | 30.6% | §9 |
| `smd.K10.incidents` | 42.3% | §9 |
| `smd.K20.incidents` | 55.1% | §9 |
| `smd.K5.movements` | 10.1% | §9 |
| `smd.K10.movements` | 19.3% | §9 |
| `smd.K20.movements` | 33.9% | §9 |
| `stats.jump_rate_with_data` | 99.6% | §10 |
| `stats.chance_overlap` | 2.6% | §5 |
| `stats.confirm_minutes_median` | 0.72 | §5 |
| `stats.false_jumps_near_fault` | 24.6% | §10 |
