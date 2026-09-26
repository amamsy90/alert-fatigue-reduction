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

## Phase 3 — Stretch (done on this branch)

| # | Change | Result on GAIA (K = 20) |
|---|---|---|
| 3.1 | **Robustness replay** (`run_robustness.py`): synthetic 0.25–2 GB steps, 1-min spikes, 30-min ramps, 3-h leaks on the real series | 0.5–2 GB and spikes ≥ 98%; 0.25 GB ≈ 0; ramps and leaks ≈ 0 for the jump and rate rules; the learned threshold rejects all 0.5 GB faults |
| 3.2 | **Slow-leak rule** (3-hour median), on by default for byte metrics | Ramps 99%, 3-h leaks 73%; 0.3 extra items a day on real data |
| 3.3 | **Learned jump ranker** (`src/jump_ranker.py`) | No gain (70.2%): the rise threshold already gives 94% precision. Kept as an experiment |
| 3.4 | **Cross-service grouping** (`GROUP_MINUTES`) | +1.3 points (71.5%) |
| 3.5 | **Carry-over budget** (`CARRY_OVER_MAX`) — the adaptive-K idea done causally | +4.1 points (74.3%); rate rule with the same carry-over 72.5%; 7/7 weeks |
| 3.6 | **Miss analysis** (`results/miss_causes_gaia.csv`) | 46% of misses are metric outages, 52% a full budget, 2% the detector |

## Still possible after submission

- A per-service jump size learned from a team's own Real marks (the 0.25 GB result shows one size does not fit all).
- Other fault types (CPU, disk, code) on another benchmark (RCAEval, TrainTicket-based sets).
- Slack / Opsgenie paging.

## Out of scope before submission

A new dataset. It is listed as future work (RCAEval, Nezha/TrainTicket) in the report.

## Timeline (one month)

| Week | Work |
|---|---|
| 1 | Phase 1 + 2 code and numbers — done |
| 2 | Phase 3, open review items, documents (md, Word, slides) — done |
| 3 | Fill name / course / date; rehearse the deck; dry-run the demo (CLI + Docker) |
| 4 | Freeze, `run_validate.py`, submit |
