# Alert Fatigue Reduction — Project Plan

**Author:** [Your name]  **Course:** [Course name]  **Date:** [Date]  **Version:** 1.1
**Sector:** Cloud microservice operations (online platforms)

---

## 1. Goal and deliverables

Build and evaluate a system for **cloud microservice on-call queues** that, under a fixed daily budget K, catches more real faults than the fixed-threshold + dedup baseline while showing fewer items — verified on GAIA MicroSS. One list per day: biggest memory jumps first, then escalate / ask / hide.

| ID | Deliverable | Status |
|---|---|---|
| D1 | Data pipeline + baseline numbers on the shared split | Done (`results/m1_gaia.csv`) |
| D2 | Alert classifier vs baseline at equal budget | Done (`results/m2_gaia.csv`) |
| D3 | Memory-jump detector vs injected faults | Done (`results/memory_gaia_jump.csv`) — 86.1% overlap |
| D4 | Shared-budget policy + whole-queue evaluation | Done (`results/queue_eval_gaia.csv`) — 72.8% at K=20 |
| D5 | Local on-call app (shadow mode, marks, saved model) | Done (`app/product/`) |
| D6 | Final report + name / course / date on documents | Open |

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
| T10 | Shared budget: jumps first, then escalate / ask | M4 | Done |
| T11 | Queue evaluation at K = 5 / 10 / 20 / 50 | M4 | Done |
| T12 | Stretch: ask only inside a jump; Real / Noise; retrain | D5 | Done |
| T13 | Final report; name / course / date | D6 | Open |

## 3. Schedule

| Weeks | Focus | Output |
|---|---|---|
| 1–2 | M1: data + baseline | D1 |
| 3–4 | M2: classifier | D2 |
| 5–6 | M3: memory jumps | D3 |
| 7 | M4: shared-budget policy | D4 |
| 8 | Product + write-up | D5, D6 |

Core evaluation is complete. Remaining write-up: author fields, Word/canvas copies if the course requires them.

## 4. Risk register

| Risk | Likelihood | Impact | Mitigation | Status |
|---|---|---|---|---|
| GAIA clocks disagree (logs CST, metrics UTC) | High | High | Convert logs to UTC on load | Fixed |
| Memory series missing or not in bytes | Medium | Medium | Classifier-only path; document units | Open for company data |
| Results depend on GAIA faults all being memory faults | High | High | Report alert-only numbers; retrain on company marks | Documented |
| Detection never beats baseline | Low | High | Jumps-first queue: 72.8% vs 0% at K=20 | Closed on this data |
| Company cannot use GAIA weights | High | High | Shadow mode + Real / Noise + saved model | Built |
| App reachable without a login | Medium | High | `APP_PASSWORD`; `/health` stays open | Built, off by default |

## 5. Dependencies

- GAIA dataset and memory csv files (`app/data/gaia_raw`, `app/data/gaia_metrics`).
- Python 3.12: pandas, scikit-learn, FastAPI, uvicorn, httpx.
- Optional: Prometheus (`PROM_URL`) for live memory.

## 6. Tracking and definition of done

- A task is **done** when its output is computed on the held-out test period, written under `results/`, and reproducible from a script.
- Final course success still lists the original 95% detection target. That target is **not met at K=20** (72.8%). At K=50 the queue reaches 86.0%, which is the jump-rule ceiling (86.1% of faults have an overlapping jump). Report that gap honestly.
