# Optional models

**Status:** all off. Version stays 1.0.5. GAIA and RCAEval catch numbers for the models below are pending: this tree was not scored on those datasets.

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

`run_model_ablation.py` compares four queues: the current causal queue, that queue plus metric-model proposals, that queue with the ranker choosing slots, and both. A variant is kept only when the 95% bootstrap interval on the per-fault catch difference (`src/eval_stats.py`) lies entirely above zero. The report also gives false items a day and catch at K = 5, 10, 20 and 50. `--walk-forward` refits on the past only, one week at a time, and adds `pooled`: one interval on every held-out fault across the folds. A fold that did not score a variant leaves that variant's pool skipped.

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
uv run --no-sync python run_model_ablation.py --data gaia --device cuda --backend xgboost --epochs 8 \
    --out results/model_ablation_gaia.json
uv run --no-sync python run_model_ablation.py --data gaia --device cuda --backend xgboost --epochs 8 \
    --walk-forward --out results/model_ablation_gaia_walk.json
```

Turn a kept model on only after that ablation's interval is entirely above zero:

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
python run_model_ablation.py --data synthetic --backend pointwise --epochs 2 \
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

The window ends at the last alert (`window_end` `last alert`): train end `2026-01-12T03:00:00+00:00`, test end `2026-01-18T03:15:01+00:00`. The ranker matches the baseline at every K because each day already fits in K, so the score gate is not applied. The metric model trained on CPU (`fell_back` false, `normal_only` true) over channels cpu, error, latency and memory. It does not turn `METRIC_MODEL` on: the keep rule for the flag is a GAIA run at K = 20, and that run is pending.

Walk-forward, two folds, K = 20. Fold 1 (5 faults, false items/day 0.5): metric model and both keep, 5 caught against 2, interval 0.6 / 0.2 / 1.0; ranker drop, 2 caught, interval 0.0 / 0.0 / 0.0. Fold 2 (1 fault): every variant caught that fault; metric model, ranker and both drop with interval 0.0 / 0.0 / 0.0. Pooled across both folds (6 faults): metric model and both keep, mean 0.5, interval 0.1667 / 0.8333; ranker drop, interval 0.0 / 0.0 / 0.0. `flags_changed` is false.

`record_run` stores torch, safetensors and XGBoost versions, the CUDA version string, and the optional-model flags under `ml` (`metric_model`, `learned_ranker`, `trace_model`, `llm_lora`, `log_encoder`).

## Model 3 — trace / service-graph root cause

RCAEval's pinned fetch (`tools/fetch_rcaeval_re23.py`) stores logs and metrics only. Traces are a separate download. Online Boutique and Train Ticket have `traces.parquet`. Sock Shop (`re2ss_*`, `re3ss_*`) does not, and the trace fetch skips those cases. The trace bytes are not in `tools/rcaeval_re23_sha256.txt`. The first download writes `traces.parquet.sha256` beside the file, under `app/data/` (not committed). A later run refuses to replace a file that no longer matches that sidecar.

`run_trace_eval.py --data rcaeval` needs `cases.parquet` from `tools/fetch_rcaeval_re23.py` before `tools/fetch_rcaeval_traces.py`. If that file is missing the eval exits and names the fetch. It does not invent a score.

Each RCAEval case is its own recording. The eval reads one `traces.parquet` at a time, and only the columns the graph uses. It builds that case's window once (pandas groupby for the rates, a merge of `parentSpanID` onto `spanID` for the edges) and reuses the graph for training, the jump baseline, and the model. Span tables are not concatenated. `--suite RE2` or `--suite RE3` selects one suite. `--max-cases N` reads the first N cases (`0` means all). Each case prints a progress line (spans, services, train or test, elapsed seconds, RSS). The target for RE2+RE3 is well under 30 minutes and under 16 GB.

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

Training and generation share `format_note_chat`. That is the tokenizer chat template when the tokenizer provides one, otherwise the Qwen chat string. The generation prefix leaves the assistant header open. The training string is that same prefix plus the template note and an end-of-text token, so the adapter learns to stop. Prompt tokens are masked with `-100`. The loaded base and the loaded adapter are cached for the process. `train_llm_lora.py` prints `train_loss` and `held_out_loss` each epoch (held-out rows are decision-time template notes after the split; they do not update weights) and stores both in `adapter.json`. The feature frame for those rows is built once. Held-out is every alert on or after the split, not a single day.

LoRA is r = 8, alpha = 16, dropout 0.05, on `q_proj` and `v_proj`. QLoRA nf4 is used when CUDA and bitsandbytes import; otherwise fp16 on CUDA or fp32 on CPU. The adapter is `adapter.safetensors` plus `adapter.json` (format `oncall-llm-lora-v1`, `pages` false) with SHA-256 sidecars. At load time the hash-checked bytes are staged as `adapter_model.safetensors` so peft does not read an unchecked file.

`run_llm_lora_rubric.py` with no flags scores template notes and loads no model. That is the CPU CI path. `--generate` scores the base and the LoRA adapter on held-out GAIA alerts (pass rate, invented numbers, injection cases). The default `--limit` is 60. `--limit 0` scores every held-out alert. Each row in the JSON includes why it failed. It exits without a score when GAIA or the weights are missing.

`LLM_LORA` does nothing unless `LLM_EXPLAIN` is also on. `explain()` tries the adapter, then the pinned GGUF, then no note. Notes stay on the shadow record. They do not change the queue and they do not page. Live paging is unchanged: `APP_PASSWORD`, a channel, the typed word `LIVE`, and an ingest job.

The rubric (`score_note`) is deterministic. A note must name every top signal, contain only numbers already in the prompt, stay within 500 characters, and must not repeat an instruction-only token from the untrusted log. A second note under an injected log must keep the same signal, kind and service structure.

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
uv run --no-sync python run_trace_eval.py --data rcaeval --device cuda --backend auto --epochs 8 --seed 42 \
    --out results/trace_eval_rcaeval.json
uv run --no-sync python run_trace_eval.py --data rcaeval --suite RE2 --max-cases 2 --device cuda --epochs 1
```

The full command reads RE2 and RE3 one case at a time and prints progress. `--suite` and `--max-cases` are optional.

`train_trace_model.py --data rcaeval` does not train on this machine. It exits and points at that eval command. Sock Shop cases are skipped. A missing `cases.parquet` exits before any score.

LoRA. Put a local snapshot of `Qwen/Qwen2.5-1.5B-Instruct` at revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` on disk. The script checks `model.safetensors` against the pin when that file is in the snapshot. Each epoch prints `train_loss` and `held_out_loss`.

```bash
uv run --no-sync python train_llm_lora.py --data gaia --device cuda --epochs 1 \
    --base /path/to/Qwen2.5-1.5B-Instruct --out results/llm_lora
uv run --no-sync python run_llm_lora_rubric.py --generate --data gaia \
    --base /path/to/Qwen2.5-1.5B-Instruct
```

`--generate` defaults to `--limit 60`. Pass `--limit 0` to score every held-out alert. `run_llm_lora_rubric.py` with no flags stays the template-only check and loads no model. Training's held-out set is every later day, built in one pass.

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

The flag stays off unless that ablation's interval lies entirely above zero. Turning the columns on is still `LLM_TEMPLATE_FEATURES=on`, plus:

```bash
export LOG_ENCODER=contrastive
export LOG_ENCODER_PATH=results/log_encoder
export LOG_ENCODER_SHA256=<sha256 of encoder.safetensors>
export LOG_ENCODER_DIM=16
```

## Synthetic results for models 3–5

Run on this VM with torch 2.14.1+cu130 and safetensors 0.8.0. CUDA was not available, so the device stayed CPU and `fell_back` is false (the commands asked for CPU, not CUDA). GAIA and RCAEval were not scored. Flags stayed off. `pages` is false.

Trace model, `python run_trace_eval.py --data synthetic --backend auto --epochs 20 --seed 42`. Geometric 2.8.1 imported, so `auto` selected `pyg`. 8 held-out faults. The same counts at K = 5, 10, 20 and 50. File: `app/results/trace_eval_synthetic.json`.

| Variant | Decision | Faults caught | False items/day | 95% interval (mean, low, high) |
|---|---|---|---|---|
| queue | baseline | 2 | 0.0 | |
| trace baseline | drop | 4 | | 0.25, 0.0, 0.625 against the queue |
| trace model | keep | 8 | no queue item | 0.75, 0.375, 1.0 against the queue; 0.5, 0.125, 0.875 against the baseline |

The synthetic interval is above zero against both. The flag stays off until the same comparison is run on RCAEval RE2/RE3. A `--backend plain` run with the same seed and epoch count had the same K = 20 counts; the file above is the Geometric run.

LoRA weights were not trained here (the 3GB snapshot was not downloaded). `python run_llm_lora_rubric.py` scored the template notes on the synthetic first week: 945 of 945 reference notes passed the rubric. A note that invented `99999` and repeated `PAGED-NOW` failed (`no_invented_numbers` false, instruction tokens `paged-now` and `reply`, structure not stable). File: `app/results/llm_lora_rubric_synthetic.json`.

Contrastive encoder. The file `app/results/log_encoder_synthetic.json` is the earlier alert-only run (`python run_log_encoder_ablation.py --data synthetic --dim 4 --epochs 15 --seed 42`): 139 training-window templates, 5 held-out faults, K = 20, both queues caught 4 (detection 0.8), false items a day 0.71 to 1.0, per-fault catch difference 0.0 / 0.0 / 0.0, decision drop. The script now scores the full queue with the window ending at the last alert and reports every `--seeds` value. The synthetic sample has no metric series, so metric items stay empty. This VM did not re-run that file after the change (CPU CI has no torch). `LOG_ENCODER` stays unset. GAIA counts are not in this file.
