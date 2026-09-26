# Roadmap — from course project to a tool other teams can use

**Branch:** `review-fixes` (your `master` is untouched).
**Reframe:** *Budgeted incident detection from metrics.* On-call gets at most K items a day. Metric jumps and monitoring gaps come first, in the order they are confirmed; log alerts only fill a small reserved share. Works with no labels on day one, and learns from on-call marks later.

Why this reframe: on GAIA, log triage alone misses about 82% of faults, and a metric-change rule catches 99.6% of the faults that have metric data. Most teams already have Prometheus metrics and almost none have labelled alerts, so a label-free metric queue is what they can actually use.

---

## Phase 1 — Honest evaluation (makes the mark safe)

| # | Change | Where | Done when |
|---|---|---|---|
| 1.1 | **Causal queue.** Jumps carry `confirm` time and `rise_at_confirm`. Items are shown first-come per day; no ranking by a peak that isn't known yet. | `src/memory_spikes.py`, new `src/queue.py` | No code path reads `peak_rise` or future points to choose what to show |
| 1.2 | **Confirmation threshold learned on the training window** (cost = 10 × miss + false). | `src/queue.py` | Chosen on week 1 only; re-chosen per walk-forward fold |
| 1.3 | **Fair baselines**: log severity, static `memory > train p99`, Prometheus-style `delta(mem[5m]) > X`. | `src/baselines_metric.py`, `run_queue_eval.py` | Headline compares against the delta rule, not 0% |
| 1.4 | **Monitoring-gap items** ("no data for 10+ min"), grouped across services. | `src/memory_spikes.py`, `src/queue.py` | The 20–23 Aug outage shows up as one item |
| 1.5 | **Stats a marker trusts**: oracle ceiling, % of ceiling, chance overlap, real time-to-confirm, misses split into *no data / budget full / below threshold*. | `src/eval_stats.py`, `run_stats.py` | All in `results/stats_gaia.csv` |
| 1.6 | **Walk-forward that tests learning**: threshold + classifier retrained per fold; sign test vs the delta rule. | `run_walkforward.py` | Folds report faults-with-data column |
| 1.7 | Small correctness fixes: spike feature look-ahead, classifier silently reusing training spikes, false-per-day denominator, configurable day boundary. | `src/features.py`, `src/classifier.py`, `src/metrics.py`, `src/config.py` | Tests cover each |
| 1.8 | `run_validate.py` asserts the new story. | `run_validate.py` | Fails if docs and csv drift |

## Phase 2 — Usable by other teams

| # | Change | Why it matters to a stranger |
|---|---|---|
| 2.1 | Score **every** alert in a batch (today half are dropped). | Otherwise the queue silently hides alerts |
| 2.2 | **Rules-only mode**: no labels → metric jumps + gaps + severity for alerts. No error, no fake "weak" labels. | Day-one install works without training data |
| 2.3 | **Relative jump mode** (`JUMP_MODE=relative`, e.g. +30% over 15-min median) and any metric name, not just memory in bytes. | 0.5 GB only makes sense for GAIA containers |
| 2.4 | **Page jumps and gaps** to PagerDuty with stable dedup keys, not only alerts. | The strong signal must reach the pager |
| 2.5 | **Safe ingest buffer** (delete only after a successful run) and an **ingest token**. | No lost alerts; no open write endpoint |
| 2.6 | **CLI**: `python -m product.cli score --alerts a.csv --metrics m.csv --out queue.csv`. | Try it on your own export in one command |
| 2.7 | Packaging: pinned requirements, `.dockerignore`, image without GAIA data, env-var reference in README. | `docker compose up` in minutes |

## Phase 3 — Stretch (only if time is left before submission)

- **Learned jump ranker**: the classifier scores jumps (rise, service, concurrent jumps elsewhere, log lines inside) instead of alerts, trained from Real/Noise marks.
- **Cross-service grouping**: jumps on several services within minutes → one incident item.
- **Adaptive K** by on-call load; per-shift day boundary.
- **Robustness replay**: synthetic 0.25 / 0.5 / 2 GB injections and slow leaks; show where the rule breaks.

## Out of scope before submission

A new dataset. It is listed as future work (RCAEval, Nezha/TrainTicket) in the report.

## Timeline (one month)

| Week | Work |
|---|---|
| 1 | Phase 1 code + new numbers (done on this branch) |
| 2 | Phase 2 code (done on this branch); update report, validation, slides |
| 3 | Phase 3 pick one; dry-run the demo on the CLI and Docker |
| 4 | Freeze, `run_validate.py`, submit |
