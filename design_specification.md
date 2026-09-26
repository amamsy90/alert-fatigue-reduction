# Alert Fatigue Reduction — Design Specification

**Author:** [Your name]  **Course:** [Course name]  **Date:** [Date]  **Version:** 1.1
**Sector:** Cloud microservice operations (online platforms)

---

## 1. Purpose and scope

This document specifies a system that reduces alert fatigue on a **cloud microservice on-call queue**. For each incoming alert the system decides to **escalate** it now, **ask a person** when it is unsure, or **suppress** it as noise. A **memory jump** on one service is one incident; logs from that service during the jump belong to it. One daily budget **K** is shared, first come first served: a jump is shown when it is confirmed, a monitoring gap when data stops, and log alerts may use at most a quarter of the slots. Nothing is chosen using information from later in the day.

The system is trained and evaluated on **GAIA MicroSS** and benchmarked against fixed severity thresholds plus exact duplicate suppression.

**In scope:** offline training and evaluation on GAIA; classification; memory-jump detection; budget-aware policy; evaluation harness; a local web app in shadow mode (webhook ingest, Prometheus memory, Real / Noise marks, saved model).
**Out of scope for the measured result:** Slack/Opsgenie; multi-tenant SaaS; real human response times (MTTA is from timestamps). PagerDuty exists in the app but is off in shadow mode and is not used in the scores below.

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
- A jump is a run of at least two hot points, gaps of at most 2 minutes. It is **confirmed** at the second hot point; `confirm` and `rise_at_confirm` are all the live queue uses.
- **Relative mode** (`JUMP_MODE=relative`): a point is hot when it is `JUMP_RATIO` (default 30%) above its recent median — for metrics that are not memory in bytes.
- **Monitoring gap**: no point for 10 minutes on a series. Gaps that start within 10 minutes on several services are one item.
- The 0.5 GB size is chosen on **week 1 only**. Week 2 is never used for that choice.
- A first version (week-1 mean + 3 standard deviations) is kept only as a comparison. It flags a service for days if the normal level drifts.
- A fault is caught when a jump on the same service overlaps the injection window. A single high point is not a jump.

On the test period this rule overlaps **86.1%** of faults and **99.6%** of the faults that have metric data (1,053 / 1,057). Shifting jumps by a random 1–6 h leaves 2.6% overlap. Mean + 3σ: **53.8%**.

### 4.5 Decision policy

One daily budget **K** for jumps and alerts together:

1. Items are shown in the order a live system learns about them: a jump at its confirmation point (only if its rise by then ≥ a threshold learned on the training window, 0.62 GB on week 1), a gap 10 minutes after the last point, an alert at its timestamp.
2. At most K items a day. Log alerts (escalate, then ask) may use at most 25% of K.
3. An alert on a service whose shown jump is still running rides on that jump and uses no slot.
4. Ask only when the alert is inside a confirmed jump and its score is above the Bayes floor.
5. Hide the rest (duplicate, below cost threshold, or "daily budget used").

Sweep K = 5, 10, 20, 50.

A fault is caught if a surfaced alert carries its label **or** a shown jump on its service overlaps its window.

At K = 20 on the test period: full queue **71.0%**, jumps with learned threshold **72.6%**, Prometheus-style rate rule **68.9%**, log severity **0%**. Best possible 84.9%.

### 4.6 Evaluation harness

- Baselines: log severity + exact duplicates; static `memory > train p99`; rate rule `delta(mem[5m]) > 0.5 GB`. All go through the same causal queue.
- Metrics: alert precision/recall; jump overlap and false jumps; reduction rate; incident detection; MTTA (injection → first shown item); false items per day; cost-weighted error (miss = 10×).
- Scripts: `run_m1.py` (baseline), `run_m2.py` (classifier), `run_memory.py` / `run_memory_clf.py` (jumps), `run_m4.py` (alert policy), `run_stretch.py` (ask-a-person, alerts only), `run_queue_eval.py` (whole queue), `run_walkforward.py`, `run_stats.py`.
- Honest limits: every GAIA fault is the same 1 GB, 600 s memory program. Time to confirm is measured from the confirming point: median 0.72 min (30 s sampling).

### 4.7 Product (local on-call app)

- FastAPI + SQLite + browser UI (local port, currently 8083). Shadow mode on by default.
- Modes: sample, GAIA, CSV upload, webhook ingest, and a CLI (`python -m product.cli score`). Optional Prometheus metrics (`PROM_URL`). Every alert of a batch is queued.
- **Rules-only mode** when there are no labels: jumps + gaps + severity for alerts. A model is trained once labels or Real/Noise marks exist.
- PagerDuty (shadow mode off) receives escalated alerts **and** shown jumps and gaps, with stable dedup keys. `INGEST_TOKEN` protects the webhook.
- Causal queue: metric jumps and gaps table + alerts. Real / Noise buttons. Marks label later runs automatically (Real window = jump span, or ±5 minutes around a Real alert; Noise overrides). Model saved to `product/data/model.pkl`; small batches use it. Retrain reminder after 7 days or new Real marks. Optional login (`APP_PASSWORD`).

## 5. Configuration

| Parameter | Default | Meaning |
|---|---|---|
| K (daily budget) | 20 | Max shown items (jumps + alerts) per day |
| Cost ratio | 10 : 1 | Missed fault vs false alarm |
| Escalation threshold | tuned on train | Min P(real) to interrupt a person |
| Memory jump size | 0.5 GB | Rise above the previous 15-minute median |
| Memory jump window | 15 minutes | Median baseline |
| Memory min points / gap | 2 points / 2 minutes | Episode clustering |
| Jump mode / ratio | absolute / 0.3 | `relative` for non-byte metrics |
| Confirmation threshold | tuned on train | Min rise at confirmation to show a jump |
| Alert share | 25% of K | Max slots for log alerts |
| Gap minutes | 10 | Silence that becomes a gap item |
| Day offset | 0 h | Hours added to UTC before cutting days |
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
| Memory not in bytes | Use `JUMP_MODE=relative` |
| Prometheus > 11,000 points | Step is widened so each series stays under 10,000 points |
| Duplicate `alert_id` | Decisions are aligned by position, not by id |
| Batch too small to train | Score with the saved model, else rules only |
| No fault labels | Rules-only mode; page says so; Real marks train a model later |
| Budget used | Remaining items hidden with that reason |
| Metrics stop | Gap item after 10 minutes |
| Scoring fails on a webhook batch | Alerts stay in the buffer |

## 8. Key design decisions

| Decision | Rationale |
|---|---|
| Cost-sensitive learning (10:1) | A missed fault is worse than an extra alert |
| Time-based split | No leakage; imitate deployment |
| Jump over recent median, not mean + 3σ | Survives a drift in the normal level; 86.1% vs 53.8% on this data |
| Jump size chosen on week 1 only | Week 2 stays a true test |
| First come, no hindsight | A live pager cannot rank by a peak it has not seen; learned threshold keeps 72.6% vs 72.9% hindsight |
| Alerts capped at 25% of K | Log noise cannot push out a later jump |
| Gaps are items | 11% of test faults fell in monitoring gaps |
| Ask only inside a jump | Uncertain alerts outside a jump filled the budget with noise |
| Human marks → retrain | A company cannot use GAIA weights unchanged |
| Shadow mode | Decisions are logged; nothing is sent to a pager |

## 9. Measured results (test period, 1,223 faults)

| Measure (K=20 unless stated) | Result |
|---|---|
| Log severity + duplicates | 0.0% |
| Static `memory > p99` | 57.2%; 3.0 false/day |
| Rate rule `delta(mem[5m]) > 0.5 GB` | 68.9%; 2.1 false/day |
| Jumps + learned threshold | **72.6%**; 1.0 false/day |
| Full queue (jumps + gaps + alerts) | **71.0%**; 1.4 false/day |
| Best possible (oracle) | 84.9% |
| Full queue at K=5 / 10 / 50 | 19.5% / 38.6% / 85.9% |
| Walk-forward mean (8 weeks) | full queue 73.4% (CI 66.2–79.7%); jumps + threshold 74.8% (8/8 wins vs rate rule); rate rule 70.7% |
| Jump overlap where metric data exists | 99.6% |
| Test faults with no log and no metric data | 10.8% |
| Fault start → jump confirmed | median 0.72 min |

## 10. Training, testing, and validation

The review record is [VALIDATION.md](VALIDATION.md). In short:

- **Train** on days 0–7 only for the main numbers: jump size, classifier, cost threshold.
- **Test** on the following 55 days, never shuffled.
- **Walk-forward:** re-learn the classifier, its threshold and the jump confirmation threshold before each later week; jump size stays 0.5 GB. Full queue mean 73.4% (95% CI 66.2–79.7%).
- **`run_stats.py`** writes the interval, sign tests vs the rate rule, per-service rates, signal counts (incl. no-metric-data), chance overlap and confirmation times.
- **`run_m1` / `run_m2` / `run_m4` / `run_stretch`** score alerts only. They are not the system result.
- **`python run_validate.py`** checks that the csv files still match the published headlines.
