# Alert Fatigue Reduction in Cloud Microservice Operations — Project Proposal

**Author:** [Your name]
**Course / Supervisor:** [Course name / supervisor]
**Date:** [Date]
**Sector:** Cloud microservice operations (online platforms)

---

## 1. Summary

This project focuses on **one sector: cloud microservice operations** — the on-call queue behind an online platform built from many small services (login, database, cache, web front ends). Those services raise a constant stream of alerts; most are noise — duplicates, or many symptoms of a single underlying fault. The on-call engineer can check only a few at a time, so genuine incidents get missed and users of the platform pay the delay. The system builds one daily queue of at most K items. The biggest **memory jumps** (a service's memory suddenly rising well above its own recent level) take the first slots; each remaining alert is **escalated**, sent to a person to **ask**, or **suppressed**. It combines classification with memory-jump detection and is evaluated on the open GAIA MicroSS dataset, which records every injected fault, against fixed severity thresholds plus exact duplicate suppression.

## 2. Problem and motivation

- A cloud microservice platform emits thousands of alerts per day; the large majority require no action.
- Alerts about the same underlying fault (often a short memory anomaly on one service) arrive as many separate items.
- On-call attention is a hard budget: an engineer can meaningfully review only a handful of alerts per hour.
- Consequence: **alert fatigue** — real incidents are missed or acknowledged late, not because detection failed, but because attention ran out. Users of the platform see slowdowns or outages.
- Today's common practice — fixed severity thresholds plus deleting exact duplicates — does not understand alert content, cannot recognize a memory jump as one incident, and does not adapt to how much attention is available.

**Goal:** on a cloud microservice on-call queue, given a fixed review budget, surface more real faults than the threshold + dedup baseline while showing people fewer items.

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

The span supports a realistic time-based evaluation: learn from the first week (3,630 alerts, 141 faults), test on the following 55 days (31,233 alerts, 1,226 faults). The logs are in China time and the memory files in UTC, so both are aligned to UTC on loading.

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
- One daily budget of K items shared by jumps and alerts. The biggest jumps take the first slots; alerts fill what is left, escalations before "ask a person". An alert inside a jump on the same service is marked for the classifier.
- Rank by expected cost so the top-K items fit the human budget; sweep K = 5, 10, 20, 50 per day.
- *Done when:* the policy dominates the baseline at every budget; MTTA ≥ 50% lower; false-alert burden ≥ 80% lower.

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

**Metrics (mapped to the required success criteria)**

| Required criterion | Metric | Target |
|---|---|---|
| Alert classification | Precision and recall vs. labelled incidents | Recall ≥ 95% at K = 20; escalation precision ≥ 90%; suppression precision ≥ 98% |
| Memory jumps | Share of injected faults overlapped by a jump on the same service; false jumps | Overlap the real fault window; ignore single-point blips |
| System effectiveness | Alert reduction rate; incident detection rate | ≥ 90% reduction; ≥ 95% detection at K = 20 |
| Human effort | MTTA reduction; false-alert burden | ≥ 50% lower MTTA; ≥ 80% fewer false alerts/day |

MTTA is measured from the fault-injection timestamp to the moment the system surfaces the corresponding incident, for the system and the baseline alike.

### Preliminary results (test period, 1,226 faults)

| Measure | Result |
|---|---|
| Memory jumps overlapping an injected fault | 86.1% (1,056 of 1,226); 256 jumps with no fault |
| First version (mean + 3σ), for comparison | 53.8% (660 of 1,226); 322 jumps with no fault |
| Baseline, faults caught at 20 items/day | 0% — the lines inside a fault are warnings, not errors |
| Learned alerts only, faults caught at 20/day | 12.9% |
| Shared queue (jumps first, then alerts), faults caught at 20/day | **72.8%**, about 18 items and 1.3 false items per day |
| Shared queue at 5 / 10 / 50 items per day | 19.2% / 38.8% / 86.0% |

Two cautions. Every GAIA fault is a memory program, so this data favours the memory rule; on a real platform some faults will not touch memory, and the alert classifier matters more. Time to first shown item is reported by the script as under a minute, but a jump is only confirmed at its second point, so about two minutes is the honest figure. The 95% detection target is not yet met at 20 items per day: most injected faults leave no log line at all, so only the memory path can find them.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Memory series missing or flat | Keep the classifier path; the jump rule needs only the previous 15 minutes, not a training week |
| Results depend on GAIA faults all being memory faults | Report the alert-only numbers separately; retrain on a company's own labels collected in shadow mode |
| Detection rate never beats baseline at any budget | Revisit features and labels before adding model complexity |
| Suppression precision < 95% | Route all low-confidence suppressions to a human by default |
| GAIA parsing/integration takes longer than planned | Start with the alert/log subset needed for M1; expand later |

## 8. Timeline (indicative)

| Weeks | Work |
|---|---|
| 1–2 | M1: data pipeline, baseline reproduction, measured baseline numbers |
| 3–4 | M2: classifier, cost-sensitive training, first comparison vs. baseline |
| 5–6 | M3: memory-jump detection on the test period |
| 7 | M4: budget-aware policy, full sweep, MTTA and false-alert analysis |
| 8 | Stretch goals, final evaluation, report writing |

## 9. Tools and resources

- Python (pandas, scikit-learn), Jupyter for analysis
- GAIA dataset (github.com/CloudWise-OpenSource/GAIA-DataSet)
- GAIA memory metric csv files (timestamp, value), one series per service
- Git for version control; all experiments reproducible from scripts

## 10. Expected outcome

A working, evaluated prototype that — at any given level of on-call attention — catches more real faults than today's threshold + dedup approach while giving engineers a much smaller, better-organized pile of alerts to read, with quantitative evidence across all four required success criteria. Alongside it, a local on-call web app that runs the same queue in shadow mode, so a platform team can collect its own labels before trusting it.
