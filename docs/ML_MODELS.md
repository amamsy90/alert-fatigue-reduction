# Optional metric model and learned ranker

**Status:** both off. Version stays 1.0.5. GAIA catch numbers are pending: this tree was not scored on GAIA.

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

`run_model_ablation.py` compares four queues: the current causal queue, that queue plus metric-model proposals, that queue with the ranker choosing slots, and both. A variant is kept only when the 95% bootstrap interval on the per-fault catch difference (`src/eval_stats.py`) lies entirely above zero. The report also gives false items a day and catch at K = 5, 10, 20 and 50. `--walk-forward` refits on the past only, one week at a time.

A synthetic run does not turn either flag on, and it does not fill in GAIA numbers. The JSON field `gaia` stays `pending: this run did not score GAIA`.

## Install

CPU CI is `uv sync --group dev` from `app/`. It does not install the `ml` group or torch.

On the laptop, from `app/`:

```bash
uv sync --group dev --group gpu --group ml
uv pip install --index-url https://download.pytorch.org/whl/cu130 \
    --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match \
    torch safetensors
```

`torch` with the `cu130` index is the wheel for an RTX 5090 (sm_120). `safetensors` is also in the `ml` group. XGBoost is in the `gpu` group. If CUDA is missing, pass `--device cpu`. The metric model records `fell_back` when `cuda` was asked and torch has no device. The ranker does the same for XGBoost.

CPU-only torch, when there is no GPU:

```bash
uv sync --group dev --group ml
uv pip install torch
```

## Train and score on GAIA

From `app/`, with `data/gaia_raw` and `data/gaia_metrics` present. These commands do not page and do not set the flags.

```bash
python train_metric_model.py --data gaia --device cuda --epochs 8 --out results/metric_model
python train_ranker.py --data gaia --backend xgboost --device cuda --out results/ranker.json
python train_ranker.py --data gaia --backend xgboost --device cuda \
    --metric-model results/metric_model --out results/ranker_both.json
python run_model_ablation.py --data gaia --device cuda --backend xgboost --epochs 8 \
    --out results/model_ablation_gaia.json
python run_model_ablation.py --data gaia --device cuda --backend xgboost --epochs 8 \
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
| ranker | drop | 1 | 0.0 | −0.2, −0.6, 0.0 |
| both | drop | 1 | 0.0 | −0.2, −0.6, 0.0 |

The metric model trained on CPU (`fell_back` false, `normal_only` true) over channels cpu, error, latency and memory. It does not turn `METRIC_MODEL` on: the keep rule for the flag is a GAIA run at K = 20, and that run is pending.

Walk-forward, two folds, K = 20. Fold 1 (5 faults): metric model keep, 5 caught against 2, interval 0.6 / 0.2 / 1.0; ranker and both drop, 1 caught, interval −0.2 / −0.6 / 0.0. Fold 2 (1 fault): every variant caught that fault; metric model, ranker and both drop with interval 0.0 / 0.0 / 0.0. `flags_changed` is false.

`record_run` stores torch, safetensors and XGBoost versions, the CUDA version string, and both flags under `ml`.
