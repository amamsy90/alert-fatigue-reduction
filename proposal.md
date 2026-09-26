# Alert Fatigue Reduction in Cloud Microservice Operations — Project Proposal

**Author:** Aishah Mohamed Alyammahi
**Course:** Software Engineering and Programming
**Date:** [Date]
**Sector:** Cloud microservice operations (online platforms)

---

## 1. Summary

This project focuses on **one sector: cloud microservice operations** — the on-call queue behind an online platform built from many small services (login, database, cache, web front ends). Those services raise a constant stream of alerts; most are noise — duplicates, or many symptoms of a single underlying fault. The on-call engineer can check only a few at a time, so genuine incidents get missed and users of the platform pay the delay. The system builds one daily queue of at most K items. **Memory jumps** (a service's memory suddenly rising well above its own recent level) and **monitoring gaps** are shown as soon as they are confirmed; log alerts may use a quarter of the slots and are **escalated**, sent to a person to **ask**, or **suppressed**. It is evaluated on the open GAIA MicroSS dataset, which records every injected fault, against the rules a team already runs: log severity with duplicate suppression, and Prometheus-style static and rate rules on memory.

## 2. Problem and motivation

- A cloud microservice platform emits thousands of alerts per day; the large majority require no action.
- Alerts about the same underlying fault (often a short memory anomaly on one service) arrive as many separate items.
- On-call attention is a hard budget: an engineer can meaningfully review only a handful of alerts per hour.
- Consequence: **alert fatigue** — real incidents are missed or acknowledged late, not because detection failed, but because attention ran out. Users of the platform see slowdowns or outages.
- Today's common practice — fixed severity thresholds plus deleting exact duplicates — does not understand alert content, cannot recognize a memory jump as one incident, and does not adapt to how much attention is available.

**Goal:** on a cloud microservice on-call queue, given a fixed review budget, catch more injected faults than severity + exact-duplicate grouping. Success is a clear win on a frozen train window, a held-out test, and a walk-forward check — not a 95% recall target. That 95% figure assumed every fault leaves a log line. On this data most do not.

## 3. Objectives

1. **Classify** each alert: does it correspond to a real fault, and is it urgent enough to interrupt a person now?
2. **Detect memory jumps.** A jump is memory rising more than 0.5 GB above the median of the previous 15 minutes on the same service, for at least two points. One jump is one incident, and the logs from that service during the jump belong to it.
3. **Decide under a budget**: rank and route alerts so the few items a human can check are the ones that matter most.
4. **Evaluate honestly** against ground truth, across a range of attention budgets, against the standard baseline.

## 4. Data — GAIA

The project uses **GAIA (Generic AIOps Atlas)**, a free public dataset:

- **Big and real** — microservice logs and per-service container memory from about two months of operation (July–August 2021): 34,863 log alerts and 1,367 injected faults in the subset used here.
- **Trustworthy labels** — every fault was injected deliberately and recorded (a program holding extra memory for about ten minutes on one service), so the correct answer is known for each incident. Evaluation does not depend on the alerts' own severity fields.
- **Free to use** — github.com/CloudWise-OpenSource/GAIA-DataSet

The span supports a realistic time-based evaluation: learn from the first week (3,630 alerts, 144 faults), test on the following 55 days (31,233 alerts, 1,223 faults). The logs are in China time and the memory files in UTC, so both are aligned to UTC on loading.

## 5. Method

The work proceeds in four milestones, each with a clear "done when" condition.

### M1 — Data pipeline and baseline
- Ingest GAIA alerts/logs/metrics; align alerts with the injected-fault records.
- Re-implement the baseline: fixed severity thresholds + exact duplicate suppression.
- Record baseline numbers (detection rate, reduction rate, MTTA) on the identical train/test split used everywhere else.

### M2 — Alert classification
- Features from alert content and context (type, source, text similarity, recent history).
- Cost-sensitive training: missing a real fault is weighed far more heavily (10×) than raising one extra alert.
- *Done when:* the classifier beats baseline precision–recall at an equal review budget on the held-out test window.

### M3 — Memory-jump episodes
- Mark a jump when memory rises more than 0.5 GB above the median of the previous 15 minutes on the same service, for at least two points (at most two minutes apart).
- The jump size is chosen on the training week only. A first version (training-week mean + 3 standard deviations) was replaced because a service whose normal level drifts is flagged for days.
- *Done when:* jumps overlap injected memory faults on the same service, and one-point blips are not reported.

### M4 — Budget-aware decision policy
- One daily budget of K items, first come first served, with no look-ahead. A jump is shown at its confirming point if its rise by then passes a threshold learned on the training week; a monitoring gap after 10 minutes of silence; log alerts use at most 25% of K, and an alert inside a shown jump rides on it.
- Rank by expected cost so the top-K items fit the human budget; sweep K = 5, 10, 20, 50 per day.
- *Done when:* the queue beats a Prometheus-style rate rule at K=20 in real time; walk-forward mean stays near the one-shot test; weaker weeks and data gaps are reported, not hidden.

### Stretch — human-in-the-loop
- Only uncertain alerts inside a memory jump are routed to a person, instead of every uncertain alert; report how decisions change when staff can handle more or fewer items.
- An on-call web app runs the queue in shadow mode (nothing is paged). People mark items Real or Noise, and those marks become labels for retraining on the company's own data.

## 6. Evaluation plan

**Protocol**
- Time-based split: train on the first week, test on the following 55 days — never random-shuffle.
- No leakage: patterns and thresholds are learned from the training window only.
- Budget sweep: results reported at K = 5, 10, 20, 50 reviews/day, not a single operating point.
- Baseline parity: the baseline runs on the identical split.
- Ground truth: GAIA's injected-fault records.

**What this project is marked on**

| Claim | How it is shown |
|---|---|
| Beats the rules a team already runs, in real time | K=20: jumps + learned threshold **72.6%** and full queue **70.2%**, vs Prometheus-style rate rule **68.9%**, static rule 57.2%, log severity 0%; fewer false items (1.0–1.6 vs 2.1 a day) |
| Not a lucky later month | Walk-forward (everything re-learned each week): jumps + threshold **74.8%**, beats the rate rule **8/8** weeks (p = 0.0039); full queue **72.5%** (95% CI 65.7–78.3%) |
| Jumps, not alert text, find the faults | **71.0%** of test faults have a jump and **no log line** |
| Near the ceiling | Best possible at K=20 is 84.9% (22 faults a day); the queue reaches 84–86% of it |
| Limits are stated | Every fault is 1 GB for 600 s; **10.8%** of test faults fall in monitoring gaps (no data), shown as gap items; fold 7 is a 2.5-day metric outage |

The 95% recall / 95% detection figures were an **early hope**. They are not the success test: only **17.9%** of test faults leave any log, and at K=20 no method can exceed 84.9%. Full argument: `FINAL_REPORT.md`.

Time to confirm is from the injection timestamp to the jump's confirming point: median **0.72 minutes**.

### Results (test period, 1,223 faults)

| Measure | Result |
|---|---|
| Jump overlap, no budget | 86.1%; **99.6%** of faults that have metric data |
| First version (mean + 3σ), for comparison | 54.2% |
| Chance overlap (jumps shifted 1–6 h) | 2.6% |
| Log severity baseline at K=20 | 0% — the lines inside a fault are warnings, not errors |
| Rate rule `delta(mem[5m]) > 0.5 GB` at K=20 | 68.9%, 2.1 false items a day |
| Full queue at K=20 | **70.2%**, about 18 items and 1.6 false items a day |
| Why faults are missed at K=20 | 46% metric outage, 52% budget full, 2% detector |
| Other fault shapes (synthetic, real series) | 0.5–2 GB steps ≥ 98%; leaks 73–99% with the leak rule; 0.25 GB not detected |
| Full queue at 5 / 10 / 50 items per day | 19.5% / 38.6% / 85.8% |
| Walk-forward (K=20) | jumps + threshold 74.8%; full queue 72.5%; rate rule 70.7% |

Cautions. Every GAIA fault is the same memory program, so this data favours a memory rule; on a real platform some faults will not touch memory, and the alert share of the budget matters more. Keep this dataset for the submission: the finding *is* that most injected faults leave no log.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Memory series missing or flat | Keep the classifier path; the jump rule needs only the previous 15 minutes, not a training week |
| Results depend on GAIA faults all being memory faults | Report the alert-only numbers separately; retrain on a company's own labels collected in shadow mode |
| Detection rate never beats baseline at any budget | Revisit features and labels before adding model complexity |
| Marker still grades the old 95% table | Proposal and report now state the measured claim; `run_validate.py` checks the csv files |
| Changing to a second public dataset late | Do not. All 1,367 injections here are memory programs; extra metric types are not in the local files |

## 8. Timeline

Core build (M1–M4, walk-forward, local app) is done. One month remains to make the mark safe.

| Remaining weeks | Work |
|---|---|
| This week | Frozen claim + stats (interval, 8/8 test, per-service, silent faults). Done: `run_stats.py` |
| Next week | Fill name / course / date; turn `FINAL_REPORT.md` into the course Word/PDF if required |
| Week after | Viva answers from section 8 of the report; 3-minute demo of the local queue |
| Last week | `python run_validate.py`; freeze; submit. No new dataset |

## 9. Tools and resources

- Python (pandas, scikit-learn), Jupyter for analysis
- GAIA dataset (github.com/CloudWise-OpenSource/GAIA-DataSet)
- GAIA memory metric csv files (timestamp, value), one series per service
- Git for version control; all experiments reproducible from scripts

## 10. Expected outcome

A working, evaluated prototype that, at a fixed daily budget, catches more injected memory faults than severity + exact-duplicate grouping, with a walk-forward interval and a stated ceiling (faults that never move memory and never write a log). Alongside it, a local on-call web app that runs the same queue in shadow mode, so a platform team can collect its own labels before trusting it.


**Beyond microservices.** The same queue can be evaluated and tuned on any timestamped metric — orders, sensors, ad clicks, notification volume — from an **Evaluate my data** page, with no code. It is tested on a synthetic online shop and on five domains of the public NAB benchmark, and always reported against a random-chance line.
