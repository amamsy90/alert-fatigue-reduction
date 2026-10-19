# GPU library and tiny LLM: roles

**Status:** Phase 0 locked. Phase 1 (optional GPU training, CPU fallback) is in the tree. Phases 2–4 are not implemented.

The queue that pages is still the cost-sensitive logistic classifier plus the jump ranker, at most K items a day, saved as a skops archive, shadow mode unless Live is armed. GPU training and a later local LLM are optional layers. Neither is allowed to become a hard dependency, and neither decides Live paging.

## Roles

| Component | Role | Not allowed to |
|---|---|---|
| GPU library (cuML, or XGBoost on a CUDA device) | Faster training and experiment sweeps. Optionally a stronger classifier, and only when that backend is requested. | Become a hard dependency. CI stays green on CPU with `uv sync --group dev`. |
| Tiny LLM (later: 0.5–3B, quantised) | Write a short "why this item" note for each shadow queue entry. Optionally turn raw log lines into a feature, after a time-split ablation. | Decide what gets paged. Live paging stays under the existing Live rules (`APP_PASSWORD`, a channel, the typed word `LIVE`, ingest jobs only). |

## What the GPU is for, honestly

Daily GAIA frames are small. One logistic fit will not show a large GPU speedup, and it can be slower once device startup is counted: the text and category transformer stays on CPU, and that is most of the fit. The place a faster head would matter is repeated refits: `run_walkforward.py`, `run_robustness.py`, `run_classifier_ablation.py`, and any later embedding pass. `app/perf/bench_accel.py` times a single fit and a small walk-forward refit sweep. `app/perf/ACCEL_BENCH.md` holds the table. GPU cells stay `N/A` when the machine has no CUDA device. A speedup near 1×, or a slowdown, is a valid result and should be written down as such.

## Requirements

- **FR-21.** No output path without a CPU fallback. `get_backend()` returns `sklearn` when CUDA or the requested library is missing. `AlertClassifier.fit` retrains with sklearn if the GPU fit fails. The object stored for `load_model` is always a scikit-learn pipeline (skops). Pickle stays banned.
- **FR-22.** The LLM never changes the queue unless an evaluated feature flag is on. That flag is Phase 2b and does not exist yet. `classifier_decisions` takes the alerts, the scores and the config, and nothing else.
- **FR-23.** No future text and no realised labels in an LLM prompt. Phase 2a must build the prompt from the same feature frame the queue uses. The realised `label` / `fault_id` columns are not model inputs today (`test_realised_label_is_not_a_model_feature`).

## Backend selection

`src/accel.get_backend()` returns `cuml`, `xgboost-gpu` or `sklearn` and logs the choice.

| Request | Result |
|---|---|
| `auto` (default, or unset `ACCEL_BACKEND`) | `cuml` if cuML imports and a CUDA device is visible, otherwise `sklearn`. |
| `cuml` | `cuml` when usable, otherwise `sklearn`. |
| `xgboost-gpu` | `xgboost-gpu` when XGBoost was built with CUDA and a device is visible, otherwise `sklearn`. |
| `sklearn` | `sklearn`. |

`xgboost-gpu` is not the automatic choice. A tree booster is a different classifier. Phase 0 keeps the logistic queue as the trusted path until someone sets `ACCEL_BACKEND=xgboost-gpu` or `AlertClassifier(..., backend="xgboost-gpu")`. An operator who has cuML installed and wants the published logistic solver unchanged sets `ACCEL_BACKEND=sklearn`.

A CUDA-enabled wheel is not treated as a GPU. XGBoost will silently train on CPU when no device is visible; that fit is refused and sklearn is used instead, so the run is not labelled `xgboost-gpu`.

### What is saved

| Backend | Training | Persisted head |
|---|---|---|
| `sklearn` | `LogisticRegression(solver="liblinear")`, as before | That pipeline. |
| `cuml` | cuML logistic regression on the sklearn feature matrix | The same `LogisticRegression`, with coefficients copied across. |
| `xgboost-gpu` | XGBoost hist booster on CUDA | `TreeEnsembleClassifier` in `src/accel.py`. skops cannot store a `Booster`. The walker is allowlisted in `product/model_store.py` (`TREE_SKOPS_TYPE`). It only reads numeric node arrays. |

liblinear and cuML do not share a solution path. The acceptance bar, checked when a CUDA device and cuML are both present, is a mean absolute difference of P(real) of at most `LOGISTIC_PROB_ATOL` (0.05) and an escalated-set Jaccard of at least `LOGISTIC_JACCARD_MIN` (0.90), fixed seed. The converted tree head must match the booster within `CONVERT_PROB_ATOL` (1e-4). Without a GPU those tests skip.

`src/tracking.accelerator_snapshot` (and every model card) records the backend name, whether each backend can run, the versions of scikit-learn, xgboost, cuml and lightgbm (null when absent), and the CUDA version from `nvidia-smi` when it answers.

## Install

Default install, and CI, do not install the `gpu` group:

```bash
cd app
uv sync --group dev
```

XGBoost, including its CUDA build on Linux wheels:

```bash
cd app
uv sync --group dev --group gpu
```

cuML is not in the lock. The wheels are tied to a CUDA major version and are published on the NVIDIA index, not as a portable PyPI extra:

```bash
uv pip install --extra-index-url https://pypi.nvidia.com "cuml-cu12"
```

Use `cuml-cu13` (or the matching CUDA major) when the driver reports that version. LightGBM's GPU build is not wired: it is not a reliable wheel, and a third tree format is not worth a second converter until a measured ablation asks for it.

## Later phases (not in this change)

- **Phase 2a.** `product/llm.py` with `explain(item_context) -> str`. Shadow notes only. Temperature 0, fixed seed, token cap, timeout. Failure produces the queue without notes. Log text is untrusted. No realised labels in the prompt.
- **Phase 2b.** Optional numeric features from log templates, time-split, kept only if `eval_stats` shows a gain. Off unless that evaluated flag is on.
- **Phase 3.** Pin the LLM by name, revision and file hash. Do not commit `*.gguf` or `*.safetensors`.
- **Phase 4.** Optional `Dockerfile.gpu`. The default image stays CPU. Metrics for LLM latency, timeout, fallback and GPU memory. Health shows the active backend.

Version stays 1.0.5 until a release that includes those phases.
