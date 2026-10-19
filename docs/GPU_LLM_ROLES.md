# GPU library and tiny LLM: roles

**Status:** Phase 0 locked. Phase 1 (optional GPU training, CPU fallback), Phase 2a (shadow notes) and Phase 3 (pinned local LLM) are in the tree. Phase 2b and Phase 4 are not implemented.

The queue that pages is still the cost-sensitive logistic classifier plus the jump ranker, at most K items a day, saved as a skops archive, shadow mode unless Live is armed. GPU training and a later local LLM are optional layers. Neither is allowed to become a hard dependency, and neither decides Live paging.

## Roles

| Component | Role | Not allowed to |
|---|---|---|
| GPU library (cuML, or XGBoost on a CUDA device) | Faster training and experiment sweeps. Optionally a stronger classifier, and only when that backend is requested. | Become a hard dependency. CI stays green on CPU with `uv sync --group dev`. |
| Tiny LLM (Qwen2.5-1.5B-Instruct, Q4_K_M GGUF) | Write a short "why this item" note beside each shadow queue entry, when `LLM_EXPLAIN` is on. A later phase may turn raw log lines into a feature, after a time-split ablation. | Decide what gets paged. Live paging stays under the existing Live rules (`APP_PASSWORD`, a channel, the typed word `LIVE`, ingest jobs only). |

## What the GPU is for, honestly

Daily GAIA frames are small. One logistic fit will not show a large GPU speedup, and it can be slower once device startup is counted: the text and category transformer stays on CPU, and that is most of the fit. The place a faster head would matter is repeated refits: `run_walkforward.py`, `run_robustness.py`, `run_classifier_ablation.py`, and any later embedding pass. `app/perf/bench_accel.py` times a single fit and a small walk-forward refit sweep. `app/perf/ACCEL_BENCH.md` holds the table. GPU cells stay `N/A` when the machine has no CUDA device. A speedup near 1×, or a slowdown, is a valid result and should be written down as such.

## Requirements

- **FR-21.** No output path without a CPU fallback. `get_backend()` returns `sklearn` when CUDA or the requested library is missing. `AlertClassifier.fit` retrains with sklearn if the GPU fit fails. The object stored for `load_model` is always a scikit-learn pipeline (skops). Pickle stays banned.
- **FR-22.** The LLM never changes the queue unless an evaluated feature flag is on. That flag is Phase 2b and does not exist yet. Shadow notes are off by default and are stored beside the would-have-paged list after the page decision. `classifier_decisions` takes the alerts, the scores and the config, and nothing else.
- **FR-23.** No future text and no realised labels in an LLM prompt. `product/llm.py` builds the note from item features, the strongest of those features, and counts of the same log template on earlier lines. The realised `label` / `fault_id` columns are not model inputs (`test_realised_label_is_not_a_model_feature`, `test_explanation_input_has_no_labels_or_later_rows`).

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

## Shadow explanations (Phase 2a and Phase 3)

`product/llm.explain` returns one short note, or `""` when it cannot. `page_job` stores the notes on `would_have_paged["explanations"]` after it has decided what to send. The item dicts (`kind`, `service`, `summary`, `dedup_key`, `severity`) stay as they are. A note is not a page, and the note text is not read back as an instruction.

Explanations are off unless `LLM_EXPLAIN` is `1`, `true`, `yes`, or `on`. The queue is unchanged either way.

```bash
cd app
python scripts/fetch_llm.py
export LLM_GGUF=product/data/llm/qwen2.5-1.5b-instruct-q4_k_m.gguf   # optional; this is the default
export LLM_EXPLAIN=1
uv pip install 'llama-cpp-python>=0.3,<1'
```

`llama-cpp-python` is not in the `dev` or `gpu` groups. CI does not install it. With the flag off, or with no weight file, the shadow record is `status: off` or `status: missing` and `notes: []`.

| Env | Default | Meaning |
|---|---|---|
| `LLM_EXPLAIN` | off | Turn notes on. |
| `LLM_GGUF` | `product/data/llm/<pinned filename>` | Local GGUF. No download at inference time. |
| `LLM_SEED` | `0` | Completion seed. |
| `LLM_MAX_TOKENS` | `64` (cap `256`) | Token cap. |
| `LLM_TIMEOUT_S` | `5` (cap `30`) | Bound on one completion. |

Temperature is `0`. It is not read from the environment. A timeout, an import error, a hash mismatch, or any other failure sets `notes` to `[]` and leaves the item list in place. Log lines are wrapped between `UNTRUSTED LOG TEXT START` and `UNTRUSTED LOG TEXT END`. Lines that look like instructions are dropped before they reach the model.

The pin is `product/llm_pin.json`: model name `Qwen2.5-1.5B-Instruct`, repo revision `91cad51170dc346986eccefdc2dd33a9da36ead9`, file `qwen2.5-1.5b-instruct-q4_k_m.gguf`, SHA-256 `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` (the Git LFS oid, 1,117,320,736 bytes). `load_model`'s rule is the pattern: hash the bytes, refuse a mismatch. The check runs when explanations are on, and the digest is cached for the process on size and mtime. A shadow record that contains a note also stores `prompt_version` (`explain-v1`) and `llm_version` (`name@revision`).

```bash
python scripts/fetch_llm.py --check product/data/llm/qwen2.5-1.5b-instruct-q4_k_m.gguf
```

`--check` exits `1` when the file is missing or the hash differs. `*.gguf` and `*.safetensors` are gitignored. `product/data/` already is, which is where the default download lands. The script writes a `<file>.sha256` sidecar only after the hash matches.

## Later phases (not in this change)

- **Phase 2b.** Optional numeric features from log templates, time-split, kept only if `eval_stats` shows a gain. Off unless that evaluated flag is on. Notes do not feed this.
- **Phase 4.** Optional `Dockerfile.gpu`. The default image stays CPU. Metrics for LLM latency, timeout, fallback and GPU memory. Health shows the active backend.

Version stays 1.0.5 until a release that includes those phases.
