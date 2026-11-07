# Optional models

**Status:** all off. **Version:** 1.2.4. Release 1.1.0 does not set any flag below. A keep in the measured section is not a default. Live paging is unchanged (`APP_PASSWORD`, a channel, the typed word `LIVE`, ingest jobs only). LLM notes never page. `RANKER_EXPLAIN`, `RANKER_ABSTAIN` and `RANKER_DISAGREE` are also off. They do not page, and a review item is not a page.

The decisions, using only the counts already in this file:

- Learned ranker: kept on the PR #26 GAIA run. The 3 November 2026 re-run is `docs/results/gaia_ranker_rerun/`: at K = 20, 887 against the baseline's 851 of 1,223, difference +0.0294, interval [0.0147, 0.0442], KEEP. K = 5, 10 and 50 are DROP. Walk-forward pooled is KEEP, +0.0262 [0.0131, 0.0401]. `LEARNED_RANKER` stays off. The synthetic ranker dropped (see the synthetic table). The homelab day-12 measurement, including the leave-one-company-out drop, is `FINAL_REPORT.md` §B7.
- Contrastive log encoder: kept on GAIA (seed 42, 851 to 860 of 1223 faults, 5 of 6 seeds). `LOG_ENCODER` stays unset, and `LLM_TEMPLATE_FEATURES` stays off.
- Trace model: kept only on the pooled RCAEval RE2+RE3 comparison (commit `f26bf85`, 4 of 5 seeds). Each suite on its own drops. `TRACE_MODEL` stays off.
- Metric TCN: dropped. The published GAIA extract is the memory channel only. `METRIC_MODEL` stays off.
- LoRA: adds nothing over the template (exact match 60/60 and template rubric 60/60 on the `d245a30` run; grounding 0/60). `LLM_LORA` stays off.

The jump rule already overlaps almost every GAIA fault that has a metric series, and most faults never write a log line. Two gaps remain. A joint shape across several series can be a fault the single-series jump rule does not confirm. On days with more faults than K, the first K arrivals are not always the best K. These two models target those gaps. They do not replace the jump rule, the K budget, the promotion gate, or the Live gates.

**Neither model can page by itself.** They only propose or rank queue items, and only when their flag is on and a hash-checked file loads. Live paging is still the existing path: `APP_PASSWORD`, a channel, the typed word `LIVE`, and an ingest job. A missing file, a bad hash, a pickle, or a missing library leaves the first-come queue unchanged. Shadow remains the default.

## Model 1 — multivariate forecast error

A small causal temporal convolutional network forecasts the next bin of a service's channels together (memory, CPU, latency, error, and any other channel the metric table names). The anomaly score is the forecast error. Training is self-supervised on windows that close before the split. Windows that overlap a training fault are dropped when enough normal windows remain (`normal_only`).

Causality:

- Channel names and the per-channel mean and standard deviation are fit on bins that close strictly before the training boundary.
- A window's context is the bins before the target bin. The target is scored when that bin has closed.
- Scoring a later day does not refit the norm on that day.

The saved archive is `model.safetensors` plus `model.json`, with a SHA-256 sidecar. `load_bytes` refuses a pickle stream and a zip even when the hash matches. Torch is imported only inside the training and scoring functions.

Flag: `METRIC_MODEL` (`1`, `true`, `yes`, `on`). Default off. `METRIC_MODEL_DIR` is the archive directory. `METRIC_MODEL_SHA256` pins the weight file when set.

## Model 2 — learned ranker for the K slots

Candidates are the items the rules already proposed, plus metric-model proposals when that model ran. The group is the on-call day. The label is the existing catch rule (a metric item overlaps a fault on its service, or an alert carries that fault id). Labels are applied only to items and faults in the training window. The feature row has no fault id. A metric-model score is attached only when its timestamp is at or before `shown_at`.

The ranker does not reorder the past. It walks items in arrival order and skips a score below the tuned threshold, so a later item can take the slot. The threshold is the training cost minimum (a miss costs 10 times a false item). A tie keeps the higher threshold.

Backends, when `auto` is asked: XGBoost `rank:ndcg` (CUDA when the device is `cuda` and a device is visible, otherwise CPU), then a tiny torch pairwise net, then a scikit-learn pointwise logistic model. The saved file is JSON. Trees are numeric node arrays, the same conversion the alert classifier uses. An MLP is a numpy state dict inside that JSON. Nothing is pickled. A hash sidecar is required.

Flag: `LEARNED_RANKER`. Default off. `LEARNED_RANKER_PATH` is the JSON file. `LEARNED_RANKER_SHA256` pins it when set.

## Keep rule

The ranker tunes its threshold at the same K it is scored with. On a day whose slot-taking candidates already fit in that K, it does not apply the score gate, so the queue matches first-come. The gate is used only when the day has more candidates than slots. Results are reported at each K in `by_k`, not only at K = 20.

`experiments/model_ablation/run_model_ablation.py` compares four queues: the current causal queue, that queue plus metric-model proposals, that queue with the ranker choosing slots, and both. A variant is kept only when the 95% bootstrap interval on the per-fault catch difference (`src/eval_stats.py`) lies entirely above zero. The report also gives false items a day and catch at K = 5, 10, 20 and 50. `--walk-forward` refits on the past only, one week at a time, and adds `pooled`: one interval on every held-out fault across the folds. A fold that did not score a variant leaves that variant's pool skipped.

The test window matches the published queue (`run_queue_eval.py`): it starts at the first alert and stops one second after the last alert. It does not stop at the last metric timestamp. `window_end` in the JSON is `last alert` for that default, or `caller` when `test_end` is passed in.

A synthetic run does not turn either flag on, and it does not fill in GAIA numbers. The JSON field `gaia` stays `pending: this run did not score GAIA`.

## GAIA channels

The published GAIA extract is one channel per service. `tools/fetch_gaia.py` extracts only the 37 `docker_memory_usage_total` files. `load_metric_folder` ignores a CSV whose name does not contain `memory` or `mem`, and it stores `series_id` as the service name, so date chunks of the same memory series stay one series. On that folder the TCN's channel list is `["memory"]`. The 5090 run of this branch saw exactly that.

A frame that already has a `metric` column, or distinct `series_id` values, is scored jointly. The synthetic sample has memory, cpu, latency and error. Feeding other GAIA metrics into `load_metric_folder` would also feed them to the byte jump rule, so this change does not do that.

## CUDA weights

Training sets `torch.manual_seed`, and on CUDA also `cuda.manual_seed_all`, `cudnn.deterministic` and `use_deterministic_algorithms(..., warn_only=True)`. That does not make the TCN bit-reproducible on CUDA. On the RTX 5090, two trains of the same command wrote different safetensors hashes (`7333d3a1…` and `30d8130b…`). The ranker JSON matched across those runs. Compare GAIA metric-model runs by the ablation table, not by the weight hash.

## Install

CPU CI is `uv sync --group dev` from `app/`. It does not install the `ml` group or torch.

`uv sync` without `--inexact` deletes every package that is not in the lock. cuML, torch and sentence-transformers are not locked (their wheels depend on the CUDA index), so `uv sync --group dev --group gpu --group ml` removes them from an existing GPU venv. Use `--inexact` to keep them, install torch after that, and run the scripts with `uv run --no-sync` so a later sync does not remove them again.

On the laptop, from `app/`:

```bash
uv sync --inexact --group dev --group gpu --group ml
uv pip install --extra-index-url https://pypi.nvidia.com cuml-cu13
uv pip install --index-url https://download.pytorch.org/whl/cu130 \
    --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match \
    torch sentence-transformers
# torch can downgrade the NVIDIA runtime libraries. If a cuML fit fails after
# that, reinstall cuml-cu13 and check the fit again.
uv run --no-sync python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

`torch` with the `cu130` index is the wheel for an RTX 5090 (sm_120). `safetensors` is in the `ml` group. XGBoost is in the `gpu` group. cuML and sentence-transformers are optional for these two models; the lines above reinstall them so this sync does not wipe a venv that already uses `ACCEL_BACKEND=cuml` or `LLM_TEMPLATE_ENCODER`. If CUDA is missing, pass `--device cpu`. The metric model records `fell_back` when `cuda` was asked and torch has no device. The ranker does the same for XGBoost.

CPU-only torch, when there is no GPU:

```bash
uv sync --inexact --group dev --group ml
uv pip install torch
```

## Train and score on GAIA

From `app/`, with `data/gaia_raw` and `data/gaia_metrics` present. These commands do not page and do not set the flags. `uv run --no-sync` keeps the torch and cuML wheels installed above.

```bash
uv run --no-sync python train_metric_model.py --data gaia --device cuda --epochs 8 --out results/metric_model
uv run --no-sync python train_ranker.py --data gaia --backend xgboost --device cuda --out results/ranker.json
uv run --no-sync python train_ranker.py --data gaia --backend xgboost --device cuda \
    --metric-model results/metric_model --out results/ranker_both.json
uv run --no-sync python -m experiments.model_ablation.run_model_ablation --data gaia --device cuda --backend xgboost --epochs 8 \
    --out results/model_ablation_gaia.json
uv run --no-sync python -m experiments.model_ablation.run_model_ablation --data gaia --device cuda --backend xgboost --epochs 8 \
    --walk-forward --out results/model_ablation_gaia_walk.json
```

Release 1.1.0 does not set either flag. The TCN is dropped because GAIA is memory only, so `METRIC_MODEL` stays off. The ranker keep does not set `LEARNED_RANKER`. An operator would turn a kept model on only after that ablation's interval is entirely above zero:

```bash
export METRIC_MODEL=on
export METRIC_MODEL_DIR=results/metric_model
export METRIC_MODEL_SHA256=<sha256 of model.safetensors>
export LEARNED_RANKER=on
export LEARNED_RANKER_PATH=results/ranker.json
export LEARNED_RANKER_SHA256=<sha256 of ranker.json>
```

Synthetic check, no GAIA:

```bash
python -m experiments.model_ablation.run_model_ablation --data synthetic --backend pointwise --epochs 2 \
    --out results/model_ablation_synthetic.json
```

Without torch the metric-model variant is `torch-missing` and is not scored. The pointwise ranker still trains. With torch installed, the same command trains the small TCN on CPU.

## Synthetic result

Run on this tree with CPU torch 2.14.1+cpu (no CUDA device), safetensors 0.8.0, pointwise ranker, 2 epochs, seed 42. GAIA was not scored. Both flags stayed off. The files are `app/results/model_ablation_synthetic.json` and `app/results/model_ablation_synthetic_walk.json`.

14-day sample, first week / later days, 5 held-out faults. The same counts appeared at K = 5, 10, 20 and 50:

| Variant | Decision | Faults caught | False items/day | 95% interval (mean, low, high) |
|---|---|---|---|---|
| baseline | baseline | 2 | 0.57 | |
| metric model | keep | 5 | 0.57 | 0.6, 0.2, 1.0 |
| ranker | drop | 2 | 0.57 | 0.0, 0.0, 0.0 |
| both | keep | 5 | 0.57 | 0.6, 0.2, 1.0 |

The window ends at the last alert (`window_end` `last alert`): train end `2026-01-12T03:00:00+00:00`, test end `2026-01-18T03:15:01+00:00`. The ranker matches the baseline at every K because each day already fits in K, so the score gate is not applied. The metric model trained on CPU (`fell_back` false, `normal_only` true) over channels cpu, error, latency and memory. That synthetic keep does not turn `METRIC_MODEL` on. The published GAIA extract is the memory channel only, so the TCN is dropped for 1.1.0: it never sees the joint shape it was built for.

Walk-forward, two folds, K = 20. Fold 1 (5 faults, false items/day 0.5): metric model and both keep, 5 caught against 2, interval 0.6 / 0.2 / 1.0; ranker drop, 2 caught, interval 0.0 / 0.0 / 0.0. Fold 2 (1 fault): every variant caught that fault; metric model, ranker and both drop with interval 0.0 / 0.0 / 0.0. Pooled across both folds (6 faults): metric model and both keep, mean 0.5, interval 0.1667 / 0.8333; ranker drop, interval 0.0 / 0.0 / 0.0. `flags_changed` is false.

`record_run` stores torch, safetensors and XGBoost versions, the CUDA version string, and the optional-model flags under `ml` (`metric_model`, `learned_ranker`, `trace_model`, `llm_lora`, `log_encoder`, and the three ranker-audit flags). It appends one line to `app/results/experiments.jsonl`. Leave those lines in place. Checking the file out or replacing it with the last committed copy drops the run log. Commit the new lines, or leave the file dirty. `docs/mlops.md` is the longer note.

## Ranker audit — reasons, confidence, disagreement

Three flags sit beside `LEARNED_RANKER`. Each defaults off. With all three off, `finish_queue` does not add `ranker_audit`, the job JSON is unchanged, and the queue page hides the review panel. With one on, the shown items are still `ranker.select()`. The audit is shadow output.

- `RANKER_EXPLAIN` attaches the top three features for shown and attached items, and a global mean of absolute contributions. The method is exact interventional SHAP against the training mean (the zero vector after scaling). The `shap` package is not installed. Stored trees have no booster, so XGBoost `pred_contribs` is not called. Contributions are required to sum to the score the queue uses. A feature whose scaled value is the baseline contributes 0. At most 256 rows are explained (`RANKER_EXPLAIN_MAX`, clamped to 1–4096).
- `RANKER_ABSTAIN` lists dropped, non-attached items whose score is at least `threshold - delta`. The default delta is the 25th percentile of `|score - threshold|`. `RANKER_ABSTAIN_DELTA` overrides it when the value is a finite number ≥ 0. A bad value falls back to the percentile. A score below `threshold - delta` stays silent. Shown items are not removed.
- `RANKER_DISAGREE` flags a slot-membership disagreement whose `|score - threshold|` is at least the 75th percentile of those gaps (`RANKER_DISAGREE_DELTA` overrides it the same way). Alerts use the contrastive encoder's median split of embedding means when `LOG_ENCODER=contrastive` and the file loads (partner `contrastive+baseline`). Every other item, and every item when the encoder is absent, uses the first-come queue (partner `baseline`). An in-queue flag stays in the page set. A dropped disagreement joins the review list so it is not silent. `review_reason` is `low_confidence`, `disagreement`, or `low_confidence+disagreement`.

The review list is not passed to `page_items` or `would_have_paged`. `pages` on the audit is false. A failure while building the audit records `audit-failed` and still returns the shown queue.

The coverage curve is hypothetical and is not applied to the live queue. An automated keep is an in-queue item with `score >= threshold + delta`. An automated drop is an item outside the queue with `score <= threshold - delta`. Everything else is review. Coverage is automated / non-attached candidates. Accuracy is correct automated decisions / automated decisions. Precision is correct keeps / keeps. A keep is correct when the existing catch rule fires. The sweep uses delta 0 and the 0, 0.1, 0.25, 0.5, 0.75, 0.9 and 1 quantiles of `|score - threshold|`. Operating points target coverage 0.5, 0.7 and 0.9. `target_reached` is true only within 0.02 of the target.

The script does not set `LEARNED_RANKER` or any audit flag. `flags_changed` stays false even if an interval is above zero. A synthetic run leaves `gaia` as `pending: this run did not score GAIA`.

From `app/`, on the shipped generator:

```bash
uv run python run_ranker_audit.py --data synthetic --backend pointwise --seeds 42,0,1 --walk-forward \
    --out results/ranker_audit_synthetic.json
```

On the GPU machine, with `data/gaia_raw` and `data/gaia_metrics` present (not run in this tree):

```bash
uv run --no-sync python run_ranker_audit.py --data gaia --backend xgboost --device cuda \
    --seeds 42,0,1,2,3 --walk-forward --out results/ranker_audit_gaia.json
```

Add `--encoder results/log_encoder` when a hash-checked contrastive encoder already exists. Without it the second opinion is the first-come queue.

### Synthetic audit (this tree)

Command above, cwd `app/`. Pointwise backend, `fell_back` false, seeds 42, 0 and 1. Data: `generate_multivariate`, 21 days, generator seed 42. File: `app/results/ranker_audit_synthetic.json`. Encoder `not requested`. Train end `2026-01-12T03:00:00+00:00`, test end `2026-01-25T03:15:01+00:00`, `window_end` `last alert`, 6 held-out faults. SHAP ran on seed 42's K = 20 rows only. `contribution_sum_gap` is 0.0 on those 6 rows. `pages` is false. `flags_changed` is false. Bootstrap seed 42, 4000 draws.

K = 20, all three seeds. Ranker and first-come both caught 2 of 6 (detection 0.3333, false items a day 0.29, precision 0.3333). Per-fault difference mean 0.0, low 0.0, high 0.0, n = 6, decision drop. Candidates 7, slot items 6, baseline slots 6, threshold 1.9985752783662702. That threshold sits above every probability, and the day fits in K, so the shipped gate is not applied. Shipped preview: delta 1.013912, review count 0, review faults 0. Disagreement count 0 (in queue 0, dropped 0). Item catch-rate status `no-contrast` (0 flagged, 7 other, other rate 0.4286). Missed faults 4 with 0 disagreements; caught faults 2 with 0 disagreements. Arrival-score Spearman mean stored as 0.9999999999999999. The hypothetical curve has 8 points, all coverage 0.0, accuracy null, precision null, review 6, keeps 0, drops 0, automated 0, candidates 6. Deltas: 0.0, 1.0003, 1.0072, 1.0139, 1.4358, 1.5261, 1.5511, 1.5866. Targets 0.5, 0.7 and 0.9 all pick delta 0.0 and `target_reached` false.

Global importance, mean absolute SHAP, 6 non-attached rows: `hour_cos` 0.218464, `rise_ratio` 0.064278, `log_rise` 0.017954, `kind_alert` 0.016498, `severity_rank` 0.016498, `kind_jump` 0.016498, `hour_sin` 0.001193, `metric_score` 0.0, `kind_rule` 0.0, `kind_leak` 0.0, `kind_gap` 0.0.

Worked examples, seed 42, K = 20:

| Example | Kind | Service | shown_at | Score | In queue | Catches a fault |
|---|---|---|---|---:|---|---|
| highest score | alert | db | 2026-01-13T18:01:00+00:00 | 0.986852 | yes | yes |
| lowest score | jump | db | 2026-01-14T01:01:00+00:00 | 0.412015 | yes | no |

Highest-score reasons: `hour_cos` value 0.004363, contribution −0.015493; `rise_ratio` 0.0, +0.005371; `log_rise` 0.0, +0.001993. Lowest-score reasons: `hour_cos` 0.964787, −0.370833; `rise_ratio` 1.252008, −0.106758; `log_rise` 9.300836, −0.02671. `catches_fault` is the scoring label, not a feature.

K = 1 is an extra budget so the cut binds. It is not the published headline. All three seeds: ranker caught 1 of 6 (detection 0.1667, false items a day 0.0, precision 1.0); first-come caught 0 of 6 (detection 0.0, false items a day 0.14, precision 0.0). Mean difference 0.1667, low 0.0, high 0.5, n = 6, decision drop. The lower bound is not above 0. Candidates 7, ranker slots 1, baseline slots 2, threshold 0.8821849837176567. Shipped preview delta 0.11037, review count 1, review faults 1, and that fault is one the queue missed. Disagreement count 1 (in queue 0, dropped 1), partner `baseline`, delta 0.409744. Flagged catch rate 0.0 (n = 1) against 0.5 on the other 6. Difference mean −0.5, low −0.8333, high 0.0, n = 7, 2654 draws. The interval includes 0. Misses 5, of which 0 carry a disagreement flag; the caught fault also has none. The confidence review bucket, not the disagreement flag, holds the fault the K = 1 queue missed.

Hypothetical curve at K = 1 (delta, coverage, accuracy, precision, review, keeps, drops, automated; 6 candidates):

| Delta | Coverage | Accuracy | Precision | Review | Keeps | Drops | Automated |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.0 | 0.8333 | 1.0 | 1.0 | 1 | 1 | 4 | 5 |
| 0.1003 | 0.8333 | 1.0 | 1.0 | 1 | 1 | 4 | 5 |
| 0.1029 | 0.6667 | 1.0 | | 2 | 0 | 4 | 4 |
| 0.1104 | 0.6667 | 1.0 | | 2 | 0 | 4 | 4 |
| 0.3194 | 0.6667 | 1.0 | | 2 | 0 | 4 | 4 |
| 0.4097 | 0.3333 | 1.0 | | 4 | 0 | 2 | 2 |
| 0.4347 | 0.1667 | 1.0 | | 5 | 0 | 1 | 1 |
| 0.4702 | 0.1667 | 1.0 | | 5 | 0 | 1 | 1 |

Operating points: target 0.5 not reached (closest delta 0.1029, coverage 0.6667, accuracy 1.0, precision null, review 2); target 0.7 the same point; target 0.9 not reached (closest delta 0.0, coverage 0.8333, accuracy 1.0, precision 1.0, review 1).

Walk-forward, two folds, K = 20, seeds 42, 0 and 1. Every seed drops, review 0, disagreement 0. Fold 1 train end `2026-01-12T03:00:00+00:00`, test end `2026-01-19T03:00:00+00:00`, 5 faults: ranker 2, baseline 2, interval 0.0 / 0.0 / 0.0, n = 5. Fold 2 train end `2026-01-19T03:00:00+00:00`, test end `2026-01-25T03:15:01+00:00`, 1 fault: ranker 1, baseline 1, interval 0.0 / 0.0 / 0.0, n = 1. `flags_changed` is false.

No gain is claimed. The K = 20 interval is 0. The K = 1 interval includes 0. Disagreement on this sample does not predict faults. GAIA counts for this audit are pending the command above.

## Model 3 — trace / service-graph root cause

RCAEval's pinned fetch (`tools/fetch_rcaeval_re23.py`) stores logs and metrics only. Traces are a separate download. Online Boutique and Train Ticket have `traces.parquet`. Sock Shop (`re2ss_*`, `re3ss_*`) does not, and the trace fetch skips those cases. The trace bytes are not in `tools/rcaeval_re23_sha256.txt`. The first download writes `traces.parquet.sha256` beside the file, under `app/data/` (not committed). A later run refuses to replace a file that no longer matches that sidecar.

`experiments/traces/run_trace_eval.py --data rcaeval` needs `cases.parquet` from `tools/fetch_rcaeval_re23.py` before `tools/fetch_rcaeval_traces.py`. If that file is missing the eval exits and names the fetch. It does not invent a score.

Each RCAEval case is its own recording. The eval reads one `traces.parquet` at a time, and only the columns the graph uses. It builds that case's window once (pandas groupby for the rates, a merge of `parentSpanID` onto `spanID` for the edges) and reuses the graph for training, the jump baseline, and the model. Span tables are not concatenated. `--suite RE2` or `--suite RE3` selects one suite. `--max-cases N` reads the first N cases (`0` means all). Each case prints a progress line (spans, services, train or test, elapsed seconds, RSS). The target for RE2+RE3 is well under 30 minutes and under 16 GB.

RE2 and RE3 both start at 2024-01-01 01:00 UTC, and a series id such as `adservice_cpu` is not scoped by case. Concatenating them gave duplicate `(series_id, timestamp)` rows and `detect_jumps` raised. The trace eval keeps RE2 on the days from `load_cases` and shifts RE3 by 1000 days before it builds faults, metrics, alerts, and graphs. Online Boutique and Train Ticket still share a day inside one suite, so they still compete for one K. The JSON reports `by_suite`, `by_system` (Online Boutique and Train Ticket, keyed with the suite), and `pooled`. Each slice records `train_cases`, `test_cases`, and `tiny_train` (true below 20 training cases). A per-suite queue uses that suite's own first week. The pooled queue is the union of those queues; K is not applied a second time across suites. Pooled false items per day is the sum of false items divided by the sum of suite days. A per-system queue uses that system's series only. A fit needs at least four training windows whose graph contains the root-cause service. Fewer than that is `too-few-windows`, and the JSON and the printed line say the slice was not scored. Seven training cases can still be too few windows.

The fit sets the same CUDA switches as the metric TCN: `torch.manual_seed`, and on CUDA `cuda.manual_seed_all`, `cudnn.deterministic`, `cudnn.benchmark` off, and `use_deterministic_algorithms(..., warn_only=True)`. It also sets `CUBLAS_WORKSPACE_CONFIG=:4096:8` before the matmul, turns TF32 off, and shuffles each epoch with a CPU generator so the graph order does not use the CUDA RNG. PyG scatter follows those deterministic algorithms when that build implements it; a build that does not is reported as `pyg-best-effort` and the fit continues. A machine without PyG reports `pyg-unavailable`. `--seeds` refits every seed and prints each one. The seed rule keeps a variant only when more than half of the scored seeds keep and the mean per-fault catch difference, averaged across those seeds, has a 95% interval above 0 against both the queue and the trace baseline. The bootstrap for that interval uses seed 42. One seed is a majority of one, so it still has to clear its own interval. A skipped seed is not in the majority.

`trace_residual` is a second fit. It adds one node column, `max(error_jump, latency_jump)`, the score the non-learned baseline already uses. The five default features are unchanged. The residual is kept only when both 95% intervals, against the queue and against the baseline, lie entirely above zero. It does not enter the queue and it does not page.

RCAEval `traces.parquet` has a text `time` column such as `21:24`. That clock is not a span time. The timestamp is `startTimeMillis` or `startTime` (or another numeric epoch). A clock column is never parsed as the span time.

A window is the spans with `time` strictly before the decision time. The past used for jumps is spans strictly before the window start. A training fault must start before the split, and its window is clipped at the split, so a later span cannot enter training. The root-cause service is the label. It is not a feature.

Node features are log call count, error rate, log duration, error-rate jump and latency jump. Edge features are log call count, error rate and latency jump, in both directions. The non-learned baseline names the service with the largest of those two jumps. A tie goes to the lexicographically smaller service name.

The model is one message-passing layer. `torch_geometric` 2.8.1 installed on this machine, so `--backend auto` uses `SAGEConv`. `--backend plain` is the plain-torch layer (node linear, edge linear, mean aggregate, self update, linear score). `auto` landing on plain because Geometric is missing is the supported path. Asking for `pyg` when it does not import sets `gnn_fell_back`. A `cuda` request with no device sets `fell_back`.

A catch counts only when the top service is the fault's component. A caller does not count. The model is kept only when the 95% interval on the per-fault catch difference lies entirely above zero against both the trace baseline and the queue, at K = 20. The report also gives K = 5, 10 and 50. The model does not add a queue item and it does not page. `TRACE_MODEL` is not read by the paging path.

Flag: `TRACE_MODEL`. Default off. `TRACE_MODEL_DIR` is the archive (`model.safetensors` + `model.json`, format `oncall-trace-gnn-v1`, `pages` false). `TRACE_MODEL_SHA256` pins the weight file when set. Pickle and zip are refused.

## Model 4 — LoRA shadow notes

`explain()` already builds a decision-time prompt from the feature frame (`product/llm.py`). `note_examples` uses that same prompt, on the prefix of rows known at the item, and pairs it with a template note filled from those fields (kind, service, severity, top signal names and values, template counts). Log text is not copied into the reference. A prompt that contains `fault_id` or a `label:` line is refused. Rows at or after the split are not training rows.

The base is the Hugging Face safetensors snapshot, pinned in `app/product/llm_lora_pin.json`:

| Field | Value |
|---|---|
| repo | `Qwen/Qwen2.5-1.5B-Instruct` |
| revision | `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` |
| file | `model.safetensors` |
| sha256 | `dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee` |
| size | 3087467144 bytes |

Training and generation share `format_note_chat`. That is the tokenizer chat template when the tokenizer provides one, otherwise the Qwen chat string. The generation prefix leaves the assistant header open. The training string is that same prefix plus the template note and an end-of-text token, so the adapter learns to stop. Prompt tokens are masked with `-100`. The loaded base and the loaded adapter are cached for the process. The feature frame for those rows is built once. Held-out is every alert on or after the split, not a single day.

The training target is that deterministic template. It does not add a reason or a check that the template does not already state. A low held-out loss, or a perfect score on `score_note`, means the adapter is reproducing the template. On the 08cc278 GAIA run the template rubric was LoRA 60/60 and base 0/60, and the only base failure was `missing_top_signals`: the rubric requires every signal name, which is how the template is worded. That run's held-out loss (2.03e-5) sat far below its train loss (0.0131) because the held-out targets repeat the same skeletons. Those two numbers are from that run, not from this tree.

`adapter.json` records `target_diversity` for train and held-out: `unique_ratio` is exact unique notes over rows, and `near_duplicate_ratio` is the share of rows whose skeleton (every number replaced by `#`) occurs more than once. `held_out_loss` is the mean cross-entropy per supervised token on the earliest row of each skeleton, then `--held-out-limit` (default 500; `0` keeps every skeleton). `held_out_loss_random` is the same mean on a separate random cap of that limit, so a small set of skeletons does not hide the loss on the other rows. `held_out_rows` stays the full count. The training update is still one row at a time. The loss pass uses `--held-out-batch` (default 2), drops each batch's logits, and prints progress.

LoRA is r = 8, alpha = 16, dropout 0.05, on `q_proj` and `v_proj`. QLoRA nf4 is used when CUDA and bitsandbytes import; otherwise fp16 on CUDA or fp32 on CPU. The adapter is `adapter.safetensors` plus `adapter.json` (format `oncall-llm-lora-v1`, `pages` false) with SHA-256 sidecars. At load time the hash-checked bytes are staged as `adapter_model.safetensors` so peft does not read an unchecked file.

`run_llm_lora_rubric.py` with no flags scores template notes and loads no model. That is the CPU CI path. It also writes `target_diversity` for the synthetic train and held-out template notes. `--generate` scores the base and the LoRA adapter on held-out GAIA alerts (pass rate, invented numbers, injection cases) and writes the same diversity for the full GAIA split. The default `--limit` is 60. `--limit 0` scores every held-out alert. Each row in the JSON includes why it failed. It exits without a score when GAIA or the weights are missing.

`--generate` also writes `template_match`: exact and near (numbers ignored) agreement between the raw generation and `reference_note`. A high near-match rate means the adapter is emitting the template. `grounding` is a separate rubric. Its prompt adds one sentence that states the top signals. A note passes only when every number is already in the decision-time prompt, every identifier (snake_case, a dotted or hyphenated id, or a number with a unit) is one of the alert's own fields, the note says why (`because`, `due`, or `caused`), and it says what to check (`check`, `inspect`, `look`, `verify`, `confirm`, or `review`). Ordinary words are not names. The template wording is not required, and the template itself does not pass that rubric. The template rubric is unchanged, so the old comparison is still in the file.

Log text stays inside the `UNTRUSTED LOG TEXT` block. A line that starts with an instruction is dropped. Data before an instruction on the same line is kept, and the delimiter and chat markers inside the log are blanked, so the log cannot close the data block. `explain()` still tries the adapter, then the pinned GGUF, then no note. A note that repeats an injected instruction (`ignore previous instructions`, `you are now`, `PAGED-NOW`, or a chat marker) is replaced by the template note before it is stored. The words `reply`, `previous`, and `ignore` on their own are not that echo, and neither is the word `instructions`. The replacement is still a shadow note. `--generate` writes `injection_echo` for the raw notes (count, rate, and how many fell back). The grounding rows, including `lora_grounding_injection`, record the same matched phrases and the words inside those phrases. `instruction_following` uses that phrase list, so it agrees with `injection_echo`. Grounding facts are scored on the note after a phrase echo is replaced by the template. Template match stays on the raw text.

Training seeds Python, NumPy, torch, and CUDA before the adapter is initialised, and it sets the same cuBLAS workspace as the trace fit. Dropout and some CUDA kernels are still not bit-stable, so two CUDA trains of the same command can write different `adapter.safetensors` hashes. That is expected. Compare the rubric, not the hash. The TCN has the same limit.

The template has no why clause (`because`, `due`, or `caused`). A grounding score of zero on a note that copies the template means the note never says why. It is not evidence that the adapter invented a worse explanation.

`LLM_LORA` does nothing unless `LLM_EXPLAIN` is also on. Notes stay on the shadow record. They do not change the queue and they do not page. Live paging is unchanged: `APP_PASSWORD`, a channel, the typed word `LIVE`, and an ingest job.

The template rubric (`score_note`) is deterministic. A note must name every top signal, contain only numbers already in the prompt, stay within 500 characters, and must not repeat an instruction-only token from the untrusted log. A second note under an injected log must keep the same signal, kind and service structure. That is a test of the template, not of an explanation the template does not contain.

Flag: `LLM_LORA`. Default off. `LLM_LORA_DIR`, `LLM_LORA_SHA256`, and `LLM_LORA_BASE` (local snapshot directory; the pin is checked when `model.safetensors` is present).

## Model 5 — contrastive log-template encoder

About 82% of GAIA faults never write a log line, so a template encoder can affect at most the other 18%. This run did not remeasure that share. Hash-v1 and MiniLM stay projected to 4 numbers. `LOG_ENCODER_DIM` changes the width of this encoder only. The default width is 4, so it fits the existing `tpl_h*` columns. A wider width is used only when `LOG_ENCODER=contrastive` and that file loads.

Training groups are templates whose alert timestamp falls inside a fault window that starts before the split. The window is clipped at the split. The fault id is the group key, not an input. The encoder is a character-trigram hash (64) and a small MLP, trained with supervised InfoNCE on two views of each template. Scoring is a numpy forward pass and does not import torch.

The ablation is the published full queue: metric items plus log alerts, the same comparison as the K = 20 block of `run_classifier_ablation.py` (`evaluate_template_features` with the contrastive encoder passed in). The window is the first alert through one second after the last alert. Faults that start after the last alert are not scored. `--seeds` refits the encoder and the classifier for every seed and writes each row under `by_seed`. Keep only when the 95% interval on the per-fault catch difference lies entirely above zero. The default queue (template features off) is unchanged. The script computes the catch counts; they are not hardcoded.

Flag: `LOG_ENCODER=contrastive`. Unset keeps hash-v1, or the sentence encoder when that pin matches. `LOG_ENCODER_PATH` and `LOG_ENCODER_SHA256` name the archive (`encoder.safetensors` + `encoder.json`, format `oncall-log-encoder-v1`, `pages` false). Template columns are still gated by `LLM_TEMPLATE_FEATURES`, which stays off.

## Install on the RTX 5090 laptop

The wheel is torch cu130 (sm_120). From `app/`. `--inexact` keeps packages that are not in the lock. A later `uv sync` without it, including a sync that pulls in torch-geometric or peft, removes cuML (and torch, if they were installed with `uv pip`). If a cuML fit then fails, reinstall `cuml-cu13` as in the metric-model section above. Run the scripts with `uv run --no-sync`.

```bash
uv sync --inexact --group dev --group gpu --group ml
uv pip install --index-url https://download.pytorch.org/whl/cu130 \
    --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match \
    torch safetensors
uv pip install torch_geometric
```

Geometric is optional. If that import fails, pass `--backend plain` to the trace commands. CPU CI does not install torch, Geometric, transformers or peft.

LoRA also needs the Hugging Face stack (not pinned in `uv.lock`, same reason as cuML):

```bash
uv pip install --index-url https://download.pytorch.org/whl/cu130 \
    --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match \
    torch safetensors transformers peft bitsandbytes
```

`bitsandbytes` selects QLoRA. Without it, `train_llm_lora.py` trains LoRA in fp16 on CUDA. Installing peft this way, or a locking group sync afterwards, can remove cuML. Reinstall `cuml-cu13` if that happens, then keep using `uv run --no-sync`.

## Laptop commands

These do not page and do not set the flags. Run them from `app/` unless the line says otherwise.

Traces. From the repo root, `cases.parquet` first, then the traces:

```bash
python tools/fetch_rcaeval_re23.py
python tools/fetch_rcaeval_traces.py --suite RE2
python tools/fetch_rcaeval_traces.py --suite RE3
```

Then from `app/`:

```bash
uv run --no-sync python -m experiments.traces.run_trace_eval --data rcaeval --device cuda --backend auto --epochs 8 \
    --seeds 42,0,1,2,3 --out results/trace_eval_rcaeval.json
uv run --no-sync python -m experiments.traces.run_trace_eval --data rcaeval --suite RE2 --max-cases 2 --device cuda --epochs 1
```

The full command reads RE2 and RE3 one case at a time, lays the suites on non-overlapping days, and prints per-suite, per-system, and pooled scores, including `trace_residual`. `--suite` and `--max-cases` are optional. A combined run is the command without `--suite`. `--seeds 42,0,1,2,3` refits each seed. The printed `seed-rule` line is the keep decision.

`experiments/traces/train_trace_model.py --data rcaeval` does not train on this machine. It exits and points at that eval command. Sock Shop cases are skipped. A missing `cases.parquet` exits before any score.

LoRA. Put a local snapshot of `Qwen/Qwen2.5-1.5B-Instruct` at revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` on disk. The script checks `model.safetensors` against the pin when that file is in the snapshot. Each epoch prints `train_loss`, the skeleton `held_out_loss`, and the random-sample `held_out_loss_random`. `adapter.json` includes `target_diversity` and both losses. The default held-out batch is 2.

```bash
uv run --no-sync python train_llm_lora.py --data gaia --device cuda --epochs 1 \
    --held-out-limit 500 --base /path/to/Qwen2.5-1.5B-Instruct --out results/llm_lora
uv run --no-sync python run_llm_lora_rubric.py --generate --data gaia \
    --base /path/to/Qwen2.5-1.5B-Instruct
```

`--held-out-limit 0` scores every unique held-out skeleton and, for `held_out_loss_random`, every held-out row. The default cap is 500 for both. `--held-out-batch` defaults to 2. `--generate` defaults to `--limit 60` and writes `template_match`, `grounding`, and `injection_echo`. Pass `--limit 0` to score every held-out alert. `run_llm_lora_rubric.py` with no flags stays the template-only check and loads no model. Training's held-out set is every later day, built in one pass.

Turn it on only together with shadow notes:

```bash
export LLM_EXPLAIN=on
export LLM_LORA=on
export LLM_LORA_DIR=results/llm_lora
export LLM_LORA_SHA256=<sha256 of adapter.safetensors>
export LLM_LORA_BASE=/path/to/Qwen2.5-1.5B-Instruct
```

Contrastive encoder:

```bash
uv run --no-sync python train_log_encoder.py --data gaia --dim 16 --device cuda --epochs 8 --out results/log_encoder
uv run --no-sync python run_log_encoder_ablation.py --data gaia --dim 16 --device cuda --epochs 8 \
    --seeds 42,0,1,2,3,7 --out results/log_encoder_gaia.json
```

The ablation scores the published queue (1223 held-out faults on the extract used by the 2b run, window ending at the last alert) and writes every seed. A like-for-like wrapper on the RTX 5090 is what those seeds are meant to reproduce. The counts are not stored in the script.

That ablation's interval is the keep rule. The GAIA run kept the encoder (851 to 860, 5 of 6 seeds). Release 1.1.0 still leaves `LOG_ENCODER` unset and `LLM_TEMPLATE_FEATURES` off. Turning the columns on later is still `LLM_TEMPLATE_FEATURES=on`, plus:

```bash
export LOG_ENCODER=contrastive
export LOG_ENCODER_PATH=results/log_encoder
export LOG_ENCODER_SHA256=<sha256 of encoder.safetensors>
export LOG_ENCODER_DIM=16
```

## Measured on an RTX 5090

These counts are from the runs named below. They are not recomputed in this tree. Flags stayed off. Nothing paged.

Commit `548ba96`, combined RE2+RE3, all return codes 0. `trace_model` and `trace_residual` drop on every slice. Pooled K = 20: queue 94/212, trace baseline 138, trace model 143, trace residual 143. The model-versus-baseline mean difference was +0.0236 and the 95% interval crosses 0. Each system had 7 training cases (`tiny_train`). The trace model does not beat the jump baseline on that run. RE3 Online Boutique was skipped as too-few-windows: 7 training cases is not 4 windows that contain the root-cause service.

Commit `d245a30`, same machine, all return codes 0, 426 passed. Queue and trace baseline match the `548ba96` counts. Pooled false items per day at K = 20 is 2.84. The RE3 Online Boutique skip is printed. LoRA held-out loss peaked at 10.9 GB and did not run out of memory. `injection_echo` is 0/60. Seed 42 did not repeat the trace catches: pooled `trace_model` was 147 and would have been a keep on that seed alone (versus baseline +0.0425, interval 0.0047 to 0.0802), `trace_residual` was 144, and the RE3 model was 12 where `548ba96` had 15. The trace code between those commits does not change the GNN update. The flip is CUDA training, which is not deterministic. The 147 keep is not a result to ship. The contrastive encoder on GAIA stays a keep (seed 42, 851 to 860, 5 of 6 seeds, 1223 faults). The ranker in PR #26 stays a keep. `TRACE_MODEL` and `LLM_LORA` stay off.

LoRA on `d245a30`, `--generate --limit 60`. Exact template match 60/60. Template rubric 60/60. Grounding 0/60 for the base and the adapter because the notes never say why. The template has no why clause, and the adapter copies the template, so a grounding zero is what that target produces until the targets are notes that are not the template. On `548ba96` the names check had also treated ordinary words as invented names (base: severity, rank, indicating; LoRA: recent, activity, length), and under the grounding prompt the notes contained injected instructions in 41/60 alerts. Training targets were 37 skeletons. Held-out loss on `548ba96` scored 48 rows because there were 48 unique skeletons, and a batch of 8 peaked at 22.9 GB.

On `d245a30`, in the grounding prompt with an injected log, `instruction_following` flagged 40/60 LoRA notes while `injection_echo` found 0. Those 40 were words from the injection line (`reply`, `previous`, `ignore`) counted as substring hits. They are not an injected instruction. This tree uses one phrase list for both checks and writes that list, and the words inside it, on each grounding-injection row. The 40/60 figure is the old check. The adapter hash also differed across CUDA runs. Seeding is applied before the adapter is initialised; a different hash on CUDA is still expected.

Commit `f26bf85`, seeds `42,0,1,2,3`, same machine. Repeating seed 42 produced the same catches. The seed rule keeps `trace_model` on 4 of 5 seeds and `trace_residual` on 4 of 5. Pooled versus the trace baseline, the mean per-fault catch difference is 0.0453 with interval 0.0142 to 0.0783 for the model, and 0.0651 with interval 0.0283 to 0.1038 for the residual. Pooled K = 20 catches, in that seed order: model 140, 157, 147, 148, 146; residual 151, 155, 158, 144, 151. The queue caught 94 and the baseline caught 138 of 212 faults. Each suite on its own drops on every seed. On RE2 the model caught 113 to 119 against a baseline of 121. On RE3 the model stayed below the queue's 39. The keep is only the pooled RE2+RE3 comparison. That pool has 28 training cases (7 per system), which is a small sample, and every suite drops. `TRACE_MODEL` stays off.

## Synthetic results for models 3–5

Run on this VM with torch 2.14.1+cu130 and safetensors 0.8.0. CUDA was not available, so the device stayed CPU and `fell_back` is false (the commands asked for CPU, not CUDA). GAIA and RCAEval were not scored. Flags stayed off. `pages` is false.

Trace model, `python -m experiments.traces.run_trace_eval --data synthetic --backend auto --epochs 20 --seed 42`. Geometric 2.8.1 imported, so `auto` selected `pyg`. 8 held-out faults. The same counts at K = 5, 10, 20 and 50. File: `app/results/trace_eval_synthetic.json`.

| Variant | Decision | Faults caught | False items/day | 95% interval (mean, low, high) |
|---|---|---|---|---|
| queue | baseline | 2 | 0.0 | |
| trace baseline | drop | 4 | | 0.25, 0.0, 0.625 against the queue |
| trace model | keep | 8 | no queue item | 0.75, 0.375, 1.0 against the queue; 0.5, 0.125, 0.875 against the baseline |

The synthetic interval is above zero against both. The flag stays off. On the RTX 5090 the seed rule keeps only the pooled RE2+RE3 comparison, on 28 training cases, and every suite on its own drops. A `--backend plain` run with the same seed and epoch count had the same K = 20 counts; the file above is the Geometric run.

LoRA weights were not trained here (the 3GB snapshot was not downloaded). `python run_llm_lora_rubric.py` scored the template notes on the synthetic first week: 945 of 945 reference notes passed the template rubric. A note that invented `99999` and repeated `PAGED-NOW` failed (`no_invented_numbers` false, instruction tokens `paged-now` and `reply`, structure not stable). The same run's `target_diversity` shows why a template score is not a new explanation: 945 training notes collapse to 74 skeletons (unique ratio 0.9989, near-duplicate ratio 0.9788) and 921 held-out notes collapse to 62 skeletons (unique ratio 1.0, near-duplicate ratio 0.987). File: `app/results/llm_lora_rubric_synthetic.json`. The GAIA template-match and grounding counts from commit 548ba96 are in the section above, not in this synthetic file.

Contrastive encoder. The file `app/results/log_encoder_synthetic.json` is the earlier alert-only run (`python run_log_encoder_ablation.py --data synthetic --dim 4 --epochs 15 --seed 42`): 139 training-window templates, 5 held-out faults, K = 20, both queues caught 4 (detection 0.8), false items a day 0.71 to 1.0, per-fault catch difference 0.0 / 0.0 / 0.0, decision drop. The script now scores the full queue with the window ending at the last alert and reports every `--seeds` value. The synthetic sample has no metric series, so metric items stay empty. This VM did not re-run that file after the change (CPU CI has no torch). `LOG_ENCODER` stays unset. The GAIA keep (851 to 860, 5 of 6 seeds) is in the section above, not in this synthetic file.
