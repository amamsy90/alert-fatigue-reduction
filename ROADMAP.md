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
| 3.3 | **Learned jump ranker** (`src/jump_ranker.py`) | No gain on this experiment (69.7%): the rise threshold already gives 94% precision. Kept as an experiment. This is not the later `LEARNED_RANKER` model. That flag stays off. Its GAIA keep has no catch count in the committed files, and the homelab measurement is `FINAL_REPORT.md` §B7 |
| 3.4 | **Cross-service grouping** (`GROUP_MINUTES`) | +1.3 points (71.5%) |
| 3.5 | **Carry-over budget** (`CARRY_OVER_MAX`) — the adaptive-K idea done causally | +3.0 points (72.6%); rate rule with the same carry-over 72.5%; 3 of 6 weeks after the look-ahead fixes |
| 3.6 | **Miss analysis** (`results/miss_causes_gaia.csv`) | 46% of misses are metric outages, 52% a full budget, 2% the detector |

## Phase 4 — Any data, no code (done on this branch)

| # | Change | Where |
|---|---|---|
| 4.1 | Generic engine: column detection (long/wide, any names), per-series split, every system tuned with the same cost, chance line, miss causes | `src/generic.py` |
| 4.2 | Unit-free robust jump rule (drops and rises), sampling-aware episode gaps | `src/memory_spikes.py` |
| 4.3 | **Evaluate my data** page: upload or pick a dataset → report → use the settings → score in the queue | `product/static/evaluate.html`, `product/app.py` |
| 4.4 | Ready-made datasets: online shop (synthetic, with notifications) and NAB (5 real domains) | `product/datasets.py`, `examples/` |
| 4.5 | CLI `evaluate` and `tune`; saved settings used by the live queue, webhook and `score` | `product/cli.py`, `product/settings.py` |
| 4.6 | End-user guide | `USE_YOUR_DATA.md` |

## Phase 5 — After the review (done on master)

| # | Change | Where |
|---|---|---|
| 5.1 | Paging watermark: only items new since the last ingest job are paged; one job at a time; job / evaluation retention; upload cap; URL credentials redacted | `product/ops.py`, `product/pipeline.py`, `product/app.py` |
| 5.2 | Slack and Opsgenie paging with the same gate and the same item list as PagerDuty | `product/notifiers.py` |
| 5.3 | Per-series jump size (the hot-point size) learned on the tuning part for series with 3+ incidents, kept only where it beats the shared size; marks feed it through the faults export | `src/generic.py` |
| 5.4 | Demo dry-run from a fresh clone | `DEMO_SCRIPT.md` |

## Still possible after submission

- Other fault types (CPU, disk, code) on another benchmark (RCAEval, TrainTicket-based sets) — see the benchmark note in `VALIDATION.md`.

## Per-series confirm gate (done)

On the training window only — week 1 in `run_window`, `train_fraction` in Evaluate my data, the early split in the product queue — each series with enough of its own faults gets a confirm rise (`tune_confirm_thresholds` in `src/queue.py`, same cost as the single threshold: 10 × miss + false). It is frozen before the test window. Other series keep the global level. The causal queue is unchanged: first come, no ranking by a later peak. The log classifier is still only for alerts. The published GAIA table in `FINAL_REPORT.md` is the earlier single-threshold run; do not replace those percentages without re-running the GAIA scripts.

## Demo notes

**Run sample** on the queue page is a one-day metric demo, not the old alerts-only file. One click writes `data/sample_product/` via `src/sample_data.generate_demo_day` and scores it with the built-in absolute memory rule (a saved Evaluate-my-data detector is ignored for that click, because that detector may be tuned in other units):

- `checkout`: +1.2 GB for 8 minutes at 09:00, with log lines inside the window
- `payments`: metrics missing 14:00–15:30 (a monitoring-gap item)
- `cart`: +1 GB over two hours from 12:00 (a slow leak; the 15-minute jump rule does not cover it)
- a few other log lines that are noise

The job reports `has_metrics=true`, `jumps_shown>=1`, and gap and leak counts (leaks only while `LEAK_WINDOW_MINUTES` is not 0). Fewer than 10 alerts, so this click stays rules-only and does not fit a confirmation threshold on the same day. The queue page shows jump / gap / leak as labelled cards and a metric filter; the escalate filter does not hide those cards.

The 14-day alerts-only generator `src.sample_data.generate` is unchanged. `run_m1.py` and the classifier smoke test still use it. Do not quote them as the system result.

Instructor questions and a five-minute script: `VIVA_ANSWERS.md`. Headline numbers stay the ones in `FINAL_REPORT.md`.

## Out of scope before submission

A new dataset. It is listed as future work (RCAEval, Nezha/TrainTicket) in the report.

## Timeline (one month)

| Week | Work |
|---|---|
| 1 | Phase 1 + 2 code and numbers — done |
| 2 | Phase 3, open review items, documents (md, Word, slides) — done |
| 3 | Fill name / course / date; rehearse the deck; dry-run the demo (Run sample now includes a jump, a gap and a leak — see `VIVA_ANSWERS.md`; CLI + Docker) |
| 4 | Freeze, `run_validate.py`, submit |
