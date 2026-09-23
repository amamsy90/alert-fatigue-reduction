# Alert Fatigue Reduction — Design Specification

**Author:** [Your name]  **Course:** [Course name]  **Date:** [Date]  **Version:** 1.1
**Sector:** Cloud microservice operations (online platforms)

---

## 1. Purpose and scope

This document specifies a system that reduces alert fatigue on a **cloud microservice on-call queue**. For each incoming alert the system decides to **escalate** it now, **ask a person** when it is unsure, or **suppress** it as noise. A **memory jump** on one service is one incident; logs from that service during the jump belong to it. One daily budget **K** is shared: the biggest jumps take the first slots; alerts fill what is left.

The system is trained and evaluated on **GAIA MicroSS** and benchmarked against fixed severity thresholds plus exact duplicate suppression.

**In scope:** offline training and evaluation on GAIA; classification; memory-jump detection; budget-aware policy; evaluation harness; a local web app in shadow mode (webhook ingest, Prometheus memory, Real / Noise marks, saved model).
**Out of scope:** live paging to PagerDuty / Slack (shadow mode only); multi-tenant SaaS; real human response times (MTTA is from timestamps).

## 2. System overview

```
alerts + memory ──> [1. Ingestion & alignment] ──> [2. Features]
                                                      │
                                                      v
[6. Evaluation / queue UI] <── [5. Shared-budget policy] <── [3. Classifier]
                                         ▲                    [4. Memory jumps]
                                         └────────────────────────┘
```

1. **Ingestion & alignment** — parse GAIA (or an uploaded / webhook batch); convert log times from China time to UTC; join alerts to injected faults.
2. **Feature extraction** — one row per alert (source, severity, text, recent counts, inside-a-jump flag).
3. **Classifier** — P(alert is a real fault). Miss cost is 10× a false alarm.
4. **Memory jumps** — a rise of more than 0.5 GB above the median of the previous 15 minutes, for at least two points.
5. **Decision policy** — one list of at most K items a day. Jumps first; then escalate; then ask (only inside a jump).
6. **Evaluation / product** — scripts score week-2 / the 55-day test period; the web app shows the same queue.

## 3. Data design

### 3.1 Inputs (GAIA MicroSS)

| Input | Use |
|---|---|
| Log / alert records | Stream to classify and route |
| Container memory csv (`timestamp`, `value`) | Jump detection; file name must contain the service and "memory" |
| Injected-fault records | Ground truth: fault id, start/end (UTC after conversion), service |

About two months (July–August 2021): **34,863** alerts and **1,367** injected faults. Learn on the first week (**3,630** alerts, **141** faults). Test on the next **55** days (**31,233** alerts, **1,226** faults).

### 3.2 Core entities

| Entity | Fields |
|---|---|
| Alert | id, timestamp (UTC), source, type, severity, message, fault_id (label, or none) |
| Spike / jump | series_id, service, start, end, peak_rise |
| Decision | alert id, action (escalate / review / suppress), confidence, reason |
| Fault (ground truth) | fault id, start, end, component, description |

There is no text-clustering "incident" group. One jump is one incident.

### 3.3 Train/test split

Time-based: **first week = train, remaining 55 days = test**. No random shuffle. Jump size (0.5 GB), cost threshold, and model weights are chosen on the training window only.

## 4. Component design

### 4.1 Ingestion and alignment

- Parse GAIA run logs and fault records; localize log clocks to Asia/Shanghai, then convert to UTC so they line up with metric timestamps (UTC milliseconds).
- Join each alert to zero or one fault (same service, time inside the injection window).
- Alerts outside every window are noise. Faults with no matching alert are kept; they cap what a log-only system can catch.

### 4.2 Feature extraction

| Group | Examples |
|---|---|
| Categorical | source, severity |
| Textual | TF-IDF over the alert message |
| Temporal | alerts from the same source in the last 5 / 15 / 60 minutes |
| Jump | `during_memory_spike`: alert time and service fall inside a jump |

### 4.3 Alert classifier

- Model: logistic regression (interpretable, fast).
- **Cost-sensitive training**: miss = 10× a false alarm (sample weights).
- Output: P(real). The escalate threshold is the week-1 score that minimizes cost, not the Bayes 1/11 floor.

### 4.4 Memory jumps

- Each series is a csv with `timestamp` and `value` (bytes). The file name contains the service and "memory".
- A point is hot when `value − median(previous 15 minutes) > 0.5 GB`.
- A jump is a run of at least two hot points, gaps of at most 2 minutes.
- The 0.5 GB size is chosen on **week 1 only**. Week 2 is never used for that choice.
- A first version (week-1 mean + 3 standard deviations) is kept only as a comparison. It flags a service for days if the normal level drifts.
- A fault is caught when a jump on the same service overlaps the injection window. A single high point is not a jump.

On the test period this rule overlaps **1,056 / 1,226** faults (**86.1%**), with **256** jumps that have no fault. Mean + 3σ: **53.8%**, 322 false jumps.

### 4.5 Decision policy

One daily budget **K** for jumps and alerts together:

1. Show the biggest jumps of the day (at most K).
2. Fill leftover slots with **escalate** (highest P(real) first), then **ask a person**.
3. Ask only when the alert is inside a jump and its score is above the Bayes floor.
4. Hide the rest (duplicate, below cost threshold, or "budget used by memory jumps").

Sweep K = 5, 10, 20, 50.

A fault is caught if a surfaced alert carries its label **or** a shown jump on its service overlaps its window.

At K = 20 on the test period: shared queue (jumps first) **72.8%** of faults, about **18** items and **1.3** false items a day. Baseline **0%**. Alerts only **12.9%**.

### 4.6 Evaluation harness

- Baseline: fixed severity thresholds + exact duplicate suppression, same split.
- Metrics: alert precision/recall; jump overlap and false jumps; reduction rate; incident detection; MTTA (injection → first shown item); false items per day; cost-weighted error (miss = 10×).
- Scripts: `run_m1.py` (baseline), `run_m2.py` (classifier), `run_memory.py` / `run_memory_clf.py` (jumps), `run_m4.py` (alert policy), `run_stretch.py` (ask-a-person, alerts only), `run_queue_eval.py` (whole queue).
- Honest limits: every GAIA fault is a memory program, so report alert-only numbers separately. A jump is confirmed at its second point (~2 minutes), not the sub-minute figure the script prints.

### 4.7 Product (local on-call app)

- FastAPI + SQLite + browser UI on port 8080. Shadow mode on by default.
- Modes: sample, GAIA, CSV upload, webhook ingest. Optional Prometheus memory (`PROM_URL`).
- Shared-budget queue: Memory jumps table + alerts. Real / Noise buttons. Marks label later runs automatically (Real window = jump span, or ±5 minutes around a Real alert; Noise overrides). Model saved to `product/data/model.pkl`; small batches use it. Retrain reminder after 7 days or new Real marks. Optional login (`APP_PASSWORD`).

## 5. Configuration

| Parameter | Default | Meaning |
|---|---|---|
| K (daily budget) | 20 | Max shown items (jumps + alerts) per day |
| Cost ratio | 10 : 1 | Missed fault vs false alarm |
| Escalation threshold | tuned on train | Min P(real) to interrupt a person |
| Memory jump size | 0.5 GB | Rise above the previous 15-minute median |
| Memory jump window | 15 minutes | Median baseline |
| Memory min points / gap | 2 points / 2 minutes | Episode clustering |
| Retrain after | 7 days | Or when new Real marks arrive |

## 6. Non-functional requirements

- **Reproducibility** — every result from a script and a fixed seed.
- **Modularity** — `src/` is the library; `product/` is the app; each runner writes `results/`.
- **Performance** — full train + test on a laptop in well under an hour.
- **Auditability** — every decision stores a reason.
- **Safety** — shadow mode does not page; bind to localhost unless a password is set.

## 7. Edge cases and error handling

| Case | Handling |
|---|---|
| Unseen alert type | Low confidence; only asked if inside a jump and a slot remains |
| No memory series | Classifier-only path; no invented jumps |
| Memory not in bytes | Jump size is wrong; values must be bytes |
| Prometheus > 11,000 points | Step is widened so each series stays under 10,000 points |
| Duplicate `alert_id` | Decisions are aligned by position, not by id |
| Batch too small to train | Score with the saved model |
| No fault labels | Weak labels from severity (demo only); page warns; Real marks replace this |
| Budget used by jumps | Remaining alerts hidden with that reason |

## 8. Key design decisions

| Decision | Rationale |
|---|---|
| Cost-sensitive learning (10:1) | A missed fault is worse than an extra alert |
| Time-based split | No leakage; imitate deployment |
| Jump over recent median, not mean + 3σ | Survives a drift in the normal level; 86.1% vs 53.8% on this data |
| Jump size chosen on week 1 only | Week 2 stays a true test |
| Shared budget, jumps first | 72.8% vs 53.4% (alerts first) at K=20 on this data |
| Ask only inside a jump | Uncertain alerts outside a jump filled the budget with noise |
| Human marks → retrain | A company cannot use GAIA weights unchanged |
| Shadow mode | Decisions are logged; nothing is sent to a pager |

## 9. Measured results (test period)

| Measure | Result |
|---|---|
| Jump overlap | 86.1% (1,056 / 1,226); 256 false jumps |
| Mean + 3σ (comparison) | 53.8% (660 / 1,226); 322 false jumps |
| Baseline at K=20 | 0% of faults |
| Alerts only at K=20 | 12.9% |
| Shared queue, jumps first, K=20 | **72.8%**; ~18 items/day; 1.3 false/day |
| Shared queue at K=5 / 10 / 50 | 19.2% / 38.8% / 86.0% |
