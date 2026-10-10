# AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices — Project Plan

*(Alert Fatigue Reduction in Cloud Microservice Operations)*

**Author:** Aishah Mohamed Alyammahi  **Course:** Software Engineering and Programming  **Date:** 10 October 2026  **Version:** 2.1
**Sector:** Cloud microservice operations (online platforms)

---

## 1. Goal and deliverables

Build and evaluate a system for **cloud microservice on-call queues** that, under a fixed daily budget K, catches more real faults than the fixed-threshold + dedup baseline while showing fewer items — verified on GAIA MicroSS. One list per day, first come first served: confirmed memory jumps, monitoring gaps, then escalate / ask / hide (alerts capped at 25% of K). Next steps: `ROADMAP.md`.

| ID | Deliverable | Status |
|---|---|---|
| D1 | Data pipeline + baseline numbers on the shared split | Done (`results/m1_gaia.csv`) |
| D2 | Alert classifier vs baseline at equal budget | Done (`results/m2_gaia.csv`) |
| D3 | Memory-jump detector vs injected faults | Done (`results/memory_gaia_jump.csv`) — 86.1% overlap |
| D4 | Shared-budget policy + whole-queue evaluation | Done (`results/queue_eval_gaia.csv`) — causal queue 69.6% (jumps + threshold 72.6%) vs rate rule 68.9% at K=20 |
| D5 | Local on-call app (shadow mode, marks, saved model) | Done (`app/product/`) |
| D6 | Final report + name / course / date on documents | Done (`FINAL_REPORT.md`; name, course and date filled on the report, plan, design and proposal) |

## 2. Work breakdown

| # | Task | Milestone | Status |
|---|---|---|---|
| T1 | Download GAIA, document schema | M1 | Done |
| T2 | Ingestion + fault alignment; China time → UTC | M1 | Done |
| T3 | Baseline (thresholds + dedup) and metric modules | M1 | Done |
| T4 | Feature extraction (including inside-a-jump) | M2 | Done |
| T5 | Cost-sensitive classifier; week-1 cost threshold | M2 | Done |
| T6 | Classifier vs baseline on the test period | M2 | Done |
| T7 | Load memory series | M3 | Done |
| T8 | Jump rule: +0.5 GB over 15-minute median, ≥2 points | M3 | Done (replaced mean + 3σ) |
| T9 | Score jump overlap on week 1 (tune) and test period | M3 | Done |
| T10 | Causal shared budget: first come, alerts ≤ 25% of K, leaks and gaps as items | M4 | Done |
| T11 | Queue evaluation at K = 5 / 10 / 20 / 50 | M4 | Done |
| T11b | Walk-forward: retrain each later week | M4 | Done (`results/walkforward_gaia.csv`, full queue 71.3%, rate rule 70.7%) |
| T12 | Stretch: ask only inside a jump; Real / Noise; retrain | D5 | Done |
| T13 | Final report; name / course / date | D6 | Done (`FINAL_REPORT.md`, author fields filled) |
| T14 | Interval, sign test vs rate rule, signal counts incl. no-metric-data | M4 | Done (`results/stats_gaia.csv`) |

## 3. Schedule

| Weeks | Focus | Output |
|---|---|---|
| 1–2 | M1: data + baseline | D1 |
| 3–4 | M2: classifier | D2 |
| 5–6 | M3: memory jumps | D3 |
| 7 | M4: shared-budget policy | D4 |
| 8 | Product + write-up | D5, D6 |

Core evaluation is complete. One month remains. Do not change the dataset.

| Remaining | Focus | Output |
|---|---|---|
| This week | Frozen claim + stats | T14 done |
| Next week | Name / course / date; Word/PDF if the course wants it | D6 |
| Week after | Viva answers + 3-minute local demo | Notes from report §8 |
| Last week | `run_validate.py`; freeze; submit | Hand-in |

## 4. Risk register

| Risk | Likelihood | Impact | Mitigation | Status |
|---|---|---|---|---|
| GAIA clocks disagree (logs CST, metrics UTC) | High | High | Convert logs to UTC on load | Fixed |
| Memory series missing or not in bytes | Medium | Medium | Classifier-only path; document units | Open for company data |
| Results depend on GAIA faults all being memory faults | High | High | Report alert-only numbers; retrain on company marks | Documented |
| Detection never beats baseline | Low | High | Causal queue beats a Prometheus rate rule (72.6% vs 68.9%, 8/8 weeks) | Closed on this data |
| Company cannot use GAIA weights | High | High | Shadow mode + Real / Noise + saved model | Built |
| App reachable without a login | Medium | High | `APP_PASSWORD`; `/health` stays open | Built, off by default |

## 5. Dependencies

- GAIA dataset and memory csv files (`app/data/gaia_raw`, `app/data/gaia_metrics`).
- Python 3.12: pandas, scikit-learn, FastAPI, uvicorn, httpx.
- Optional: Prometheus (`PROM_URL`) for live memory.

## 6. Tracking and definition of done

- A task is **done** when its output is computed on the held-out test period, written under `results/`, and reproducible from a script.
- Course success is a win over the baseline under budget K, a walk-forward interval, and stated limits. The old 95% detection target is **not the claim**: at K=20 the best possible is 84.9%. At K=50 the queue reaches 85.8%. Of the misses at K=20, 46% are metric outages and 52% are a full budget; 2% are the detector. 10.8% of test faults fall in monitoring gaps (no metric data).
