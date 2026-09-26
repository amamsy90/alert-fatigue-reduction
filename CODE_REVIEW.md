# Project review — Alert Fatigue Reduction (A to Z)

> **Status (branch `review-fixes`):** all High items and most Medium items below are fixed (still open: pickle model format, Noise marks matched by timestamp, and the alert policy's own day cut is still UTC — the queue's is configurable). The Low items are open. The numbers in the report now come from the causal queue. Test faults are now 1,223 (not 1,226) because alerts, faults and metrics share one split time. See `ROADMAP.md` for what was done and what is next.

**Scope:** every file in `project_SW` — docs, `app/src`, `app/run_*.py`, `app/product`, tests, results.
**Method:** read all code, ran the test suite (46/46 pass), reproduced the headline (72.8% at K=20 — matches), then ran new stress tests on the same GAIA data. Scripts: `app/review_experiments.py`, `app/review_online_walkforward.py`.

---

## 0. Summary

The engineering is clean, reproducible and well-tested. The **evaluation story** is the weak point, and three findings change what the report should say:

1. **The baseline is a straw man.** A standard Prometheus rule, `delta(memory[5m]) > 0.5 GB`, catches **85.9%** of test faults with no cap and **68.8%** at K=20. The proposed jump rule catches 86.1% / 72.8%. The honest comparison is **72.8% vs 68.8%**, not 72.8% vs 0%.
2. **The queue uses hindsight inside each day.** It ranks each day's jumps by their *final* peak, which is only known after the day ends. A causal (online) queue — first-come, no look-ahead — scores **67.7%**. The good news: a causal fix (below) scores **73.2%** with fewer false items (0.8/day vs 1.3/day), so the claim survives if the method is changed.
3. **"Silent faults" are really missing data.** Of the 172 test faults with no jump, **170 have zero memory data points** during the fault. They are monitoring outages, not faults that "never move memory." Where data exists, the jump rule catches **1,056 / 1,060 (99.6%)**. Fold 7's drop (52.6%) is a **2.5-day outage on all ten services (20–23 Aug)**: only 61% of that week's faults have any metric data. The report's explanation ("fewer qualifying jumps") is wrong.

There is also a hard ceiling nobody reports: GAIA injects **22.3 faults/day**, more than K=20 on 34 of 55 days. The best possible score at K=20 is **84.9%** (K=10: 44.5%, K=5: 22.4%). So 72.8% is **86% of the achievable** — that's a stronger way to put it.

---

## 1. Findings on the metrics

| # | Claim in the report | What the data shows | Fix |
|---|---|---|---|
| M1 | Baseline catches 0%, so the queue wins 8/8 weeks (p = 0.0039) | The baseline only reads log **errors**; GAIA has 62 ERROR lines in 36k. It can't see memory at all, so 0% is guaranteed. A sign test against a constant 0 proves nothing. | Add metric baselines: static `memory > week-1 p99` (57.2% at K=20) and `delta(mem[5m]) > 0.5 GB` (68.8%). Keep the log baseline as a third row. |
| M2 | Jumps ranked "biggest first, at most K per day" | `peak_rise` is the max of the **whole** episode, and the day's ranking needs the whole day. An on-call person gets items in real time. | Use rise **at confirmation** (2nd hot point) and first-come order. Threshold tuned on week 1 = **0.65 GB** → walk-forward mean **75.0%** (vs 74.8% hindsight, 69.3% plain FIFO). |
| M3 | 11.3% of faults are silent — "nothing in this system can catch them" | 170/172 missed faults have no metric rows in their window. | Say "metric data missing." Better: add a **stale-metrics alert** (dead-man's switch) as a queue item — that's what a real SRE would want. |
| M4 | Fold 7 weaker because "fewer qualifying jumps" | All ten series have a 2 d 12 h gap starting 20 Aug 13:18 UTC. | Report fold 7 as a data outage; show a per-week "faults with metric data" column (100%, 91%, 94%, 82%, 84%, 92%, **61%**, 92%). |
| M5 | Confirm time "about two minutes" (0.23 printed) | Sampling is every 30 s. Real median from fault start to 2nd hot point = **0.72 min**, p90 0.93 min. | Replace "~2 min" with the measured 0.7 min, computed from the confirmation timestamp, not `jump.start`. |
| M6 | 95% CI 67.3–80.8% from walk-forward | The jump rule has no fitted parameters per fold, and alerts add **zero** catches at K=20 (jumps-only = shared = 72.8%). So "retraining each week" changes nothing that is scored. The CI is a spread of 8 weekly rates of a fixed rule. | Call it "per-week variation of a fixed rule," not a validation of learning. |
| M7 | Not stated | Chance overlap (jumps shifted 1–6 h at random) = **2.6%**. | Add this — it shows the 86% is real and not an artifact of wide windows. Good evidence for the viva. |
| M8 | Not stated | Every one of the 1,367 injections is identical: **1 GB for 600 s**. The 0.5 GB threshold is simply half the injected size. | State it plainly. It is the biggest external-validity limit. |
| M9 | False items/day compared | `false_alert_burden_per_day` divides by the span between the first and last *surfaced* item, not the test period. | Divide by test days (as `run_queue_eval._score` already does). |

**Metrics to add:** % of oracle ceiling at each K; precision of shown items; time-to-confirm distribution; misses split into *data gap / budget full / below threshold*; cost per day = 10 × misses + false items.

---

## 2. Code bugs

### High

| File | Bug | Effect |
|---|---|---|
| `product/pipeline.py` `_train` / `score_alerts` | Every batch is split, and only the **later half** is scored and stored. | Verified: sample mode ingests 1,866 alerts over 14 days; the queue shows **921 alerts over 7 days**. Half the alerts never appear. Small batches (< 7 days) instead get scored **in-sample** on the data the model was trained on. |
| `product/pagerduty.py` | Only escalated **alerts** are paged. Memory jumps are never sent. | Jumps are the part that catches ~all faults. With shadow mode off, PagerDuty would get almost none of them. |
| `product/app.py` `/ingest/*` with `score_now` | `drain_buffer()` deletes the rows before `run_job`; on an exception they're gone. | Alerts lost silently on any scoring error. Drain inside a transaction, or re-buffer on failure. |
| `product/app.py` + `docker-compose.yml` | Login is off unless `APP_PASSWORD` is set, yet the container binds `0.0.0.0`. `/ingest` accepts anything. | Anyone who can reach the port can inject alerts or mark feedback. Refuse to start without a password when not on localhost, or require a token on `/ingest`. |

### Medium

| File | Bug |
|---|---|
| `src/features.py` | `during_memory_spike` uses `spike.end`, so an alert between the first hot point and confirmation is flagged before the jump is known. Small look-ahead. Use `spike.confirm <= t <= spike.end`. |
| `src/classifier.py` `predict_proba` | If `spikes` is omitted it silently reuses the **training** spikes, so test alerts get the wrong feature (almost always 0). Make `spikes` required, or raise. |
| `product/pipeline.py` `_train` | Weak labels write `fault_id = "weak"` **into the alert objects**, which then flow to the store and to later feedback logic. Copy before labelling. |
| `src/policy.py` / all runners | Days are cut at UTC midnight (08:00 in China, where GAIA ran). Budgets per shift would be more realistic. Make the day boundary a config value. |
| `product/pipeline.py` `apply_feedback` | Noise marks match on `(source, timestamp.isoformat())`. Any change in timestamp formatting between jobs breaks the match. Match on `alert_id`. |
| `product/model_store.py` | Model saved with `pickle`. Fine locally; in production use `skops` or `joblib` plus a hash check. |
| `app/Dockerfile` | `COPY data ./data` puts ~55 MB of GAIA data into the image (there's no `.dockerignore`), and compose mounts it anyway. |

### Low / hygiene

- `src/gaia_loader.py`: row-by-row `iterrows` + per-row timezone conversion; `_label_alerts` and `ingestion.align_alerts_to_faults` are O(alerts × faults). Vectorise with `pd.to_datetime(...).dt.tz_localize` and `merge_asof`.
- `src/memory_spikes.py`: the module docstring still describes the old mean + 3σ rule.
- `src/ingestion.py`: `TODO(T1)` column map and an unused `matched_faults` set.
- `src/policy.meets_gate` needs a baseline MTTA, which is always `None` here, so the gate can never pass.
- `requirements.txt` isn't pinned. Tests pass on pandas 3.0 / sklearn 1.8, but pin the versions you submit with.
- Leftovers: `app/tests/_tmp_queue.db`, `app/product/data/queue.db` (85 MB, ignored but on disk), `__pycache__`, `.git/cursor/`. Tests should write to `tmp_path`.
- Docs say **40** tests; there are **46**.

---

## 3. Making it stronger (keep the project, fix the claim)

In order of value for the mark:

1. **Swap in the causal queue.** Rank by rise at confirmation (≥ 0.65 GB, tuned on week 1), first-come per day. Same score, fewer false items, and no hindsight. ~20 lines in `run_queue_eval.py` and `store.get_jumps`.
2. **Honest baseline table:** log severity (0%), static threshold (57.2%), Prometheus delta rule (68.8%), yours (73.2%) — all at K=20, all causal. A 4–5 point win over a real rule, with fewer false items, is a defensible result. "0% vs 73%" invites the marker's first question.
3. **Report % of oracle.** 73.2 / 84.9 = 86% of what any detector could do at K=20.
4. **Stale-metric items.** Add a queue item for "no metric data for > 10 min on service X". On GAIA it would have flagged the 20–23 Aug outage at once, and it turns the "silent" 11% into something the system handles.
5. **Use the classifier where it helps.** Today it adds nothing at K=20. Turn it into a **jump ranker**: features = rise at confirmation, service, jumps on other services in ±15 min, log lines inside. 88 of 256 false jumps happen within 15 min of a fault on *another* service — cross-service context could suppress them (or reveal mislabelled service names in GAIA; worth checking).
6. **Robustness test.** Every injection is 1 GB. Replay the rule on synthetic injections of 0.25 / 0.5 / 2 GB and on slow leaks, and show where it breaks. This addresses the external-validity question without changing datasets.

---

## 4. Better or new directions

If you want the project to be more than "a memory threshold that works on a memory-only benchmark":

- **Budget-aware incident detection (reframe).** The real contribution is the finding: *log-based triage misses ~82% of faults on GAIA because they never write a log; a cheap metric-change rule plus a shared budget catches 86% of what's achievable.* Title and abstract should say that.
- **Observability-gap alerting.** Treat missing telemetry as a first-class incident. Small, novel-sounding, directly motivated by your own data (12% of faults fell in gaps).
- **Adaptive budget.** K is fixed, but faults per day reach 32 (median 23). Let K follow the on-call load, or reserve slots for high-confidence items, and report cost per day.
- **Cross-service incident grouping.** Merge jumps on several services within minutes into one item — fewer items per incident, and the core "one incident, one item" idea from the proposal actually implemented.
- **Multi-fault generalisation** (future work only — the plan rightly says not to switch datasets in the last month). Candidates to check later: the rest of GAIA MicroSS (confirm which metric types it has), RCAEval, or the Nezha/TrainTicket benchmarks, which include CPU, network and code faults.

---

## 5. What to change in the documents

- `FINAL_REPORT.md` §5–7, `VALIDATION.md`, `proposal.md` §6: replace the 0% headline with the baseline table from §3.2; replace "silent" with "metric data missing"; rewrite fold 7; "~2 min" → "~0.7 min"; add oracle ceiling and chance overlap; state that all injections are 1 GB × 600 s.
- `run_validate.py`: it currently asserts the *old* story (e.g. that fold 7 did not fill the budget). Update the asserts once the numbers change.
- Test count 40 → 46.

---

## Appendix — reproduced and new numbers (test period, 1,226 faults, 55 days)

| Queue at K = 20 | Caught | Items/day | False/day | Causal? |
|---|---|---|---|---|
| Log severity baseline | 0.0% | 0.8 | 0.8 | yes |
| Static memory > week-1 p99, first 20/day | 57.2% | 15.9 | 2.9 | yes |
| Prometheus `delta(mem[5m]) > 0.5 GB`, first 20/day | 68.8% | 17.4 | 2.1 | yes |
| Jump rule, first 20/day | 67.7% | 17.5 | 2.4 | yes |
| **Current:** jump rule, top 20/day by final peak | 72.8% | 17.5 | 1.3 | **no** |
| **Proposed:** jump rule, rise at confirm ≥ 0.7 GB, first 20/day | **73.2%** | 17.1 | **0.8** | yes |
| Oracle (every fault caught, K cap only) | 84.9% | — | 0 | — |

Walk-forward (8 weeks): current 74.8%, online FIFO 69.3%, online tuned (0.65 GB on week 1) **75.0%**.
Chance overlap 2.6%. Confirm time median 0.72 min. Faults with metric data: 1,060 / 1,226; jump rule catches 1,056 of them.
