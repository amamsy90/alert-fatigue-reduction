# GPU library and tiny LLM: roles

**Status:** Phase 0 locked. Phase 1 (optional GPU training), Phase 2a (shadow notes), Phase 2b (optional template features, dropped for the default queue), Phase 3 (pinned local LLM) and Phase 4 (optional CUDA image, health fields, LLM and GPU metrics) are in the tree. The default image stays CPU. Explanations and template features stay off.

The queue that pages is still the cost-sensitive logistic classifier plus the jump ranker, at most K items a day, saved as a skops archive, shadow mode unless Live is armed. GPU training and a later local LLM are optional layers. Neither is allowed to become a hard dependency, and neither decides Live paging.

## Roles

| Component | Role | Not allowed to |
|---|---|---|
| GPU library (cuML, or XGBoost on a CUDA device) | Faster training and experiment sweeps. Optionally a stronger classifier, and only when that backend is requested. | Become a hard dependency. CI stays green on CPU with `uv sync --group dev`. |
| Tiny LLM (Qwen2.5-1.5B-Instruct, Q4_K_M GGUF) | Write a short "why this item" note beside each shadow queue entry, when `LLM_EXPLAIN` is on. Optional template-hash columns (`LLM_TEMPLATE_FEATURES`) were measured and are not part of the default queue. | Decide what gets paged. Live paging stays under the existing Live rules (`APP_PASSWORD`, a channel, the typed word `LIVE`, ingest jobs only). |

## What the GPU is for, honestly

Daily GAIA frames are small. One logistic fit will not show a large GPU speedup, and it can be slower once device startup is counted: the text and category transformer stays on CPU, and that is most of the fit. The place a faster head would matter is repeated refits: `run_walkforward.py`, `run_robustness.py`, `run_classifier_ablation.py`, and any later embedding pass. `app/perf/bench_accel.py` times a single fit and a small walk-forward refit sweep. `app/perf/ACCEL_BENCH.md` holds the table. GPU cells stay `N/A` when the machine has no CUDA device. A speedup near 1×, or a slowdown, is a valid result and should be written down as such.

## Requirements

- **FR-21.** No output path without a CPU fallback. `get_backend()` returns `sklearn` when CUDA or the requested library is missing. `AlertClassifier.fit` retrains with sklearn if the GPU fit fails. The object stored for `load_model` is always a scikit-learn pipeline (skops). Pickle stays banned.
- **FR-22.** The LLM never changes the queue unless `LLM_TEMPLATE_FEATURES` is on. That flag is off. The time-split ablation did not clear the bar to turn it on (see below). Shadow notes stay off by default and are stored beside the would-have-paged list after the page decision. `classifier_decisions` takes the alerts, the scores and the config, and nothing else.
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

## Template features (Phase 2b) — drop for the default queue

`LLM_TEMPLATE_FEATURES` (`1` / `true` / `yes` / `on`) adds five numeric columns to the alert classifier: `tpl_h0`–`tpl_h3`, a SHA-256 of the normalised log template mapped into [-1, 1), and `tpl_prior`, how many earlier rows share that template. A later row does not change an earlier row. The realised fault id is not an input. Shadow notes do not feed these columns.

The default encoder is `hash-v1` (no weights). A sentence-transformer file is used only when both are set:

- `LLM_TEMPLATE_ENCODER` — path to the weight file
- `LLM_TEMPLATE_ENCODER_SHA256` — expected SHA-256

A missing file, a hash mismatch, or a missing `sentence-transformers` install skips that file and uses `hash-v1`. Nothing is downloaded at train or score time. The flag is off when the variable is unset, and a model saved with it off still scores with the current columns after it is loaded.

`run_classifier_ablation.py` trains on the first week and scores the later days, the same split as the other classifier ablations. At K = 20 it can also score the GAIA full queue (jumps, leaks, gaps, and log alerts) and write `results/template_feature_ablation_gaia.json` without changing `classifier_ablation_gaia.csv`. The keep rule is the project's 95% bootstrap interval (`src.eval_stats.bootstrap_mean_ci`) on the per-fault catch difference. Keep only when the whole interval is above zero.

This machine has no `data/gaia_raw` or `data/gaia_metrics`, so that full-queue run was not done. `run_validate.py` still checks the published GAIA report numbers; it does not measure this feature.

What was run: `python run_classifier_ablation.py --template-features` on the built-in synthetic sample (`sample_data.generate`, seed 42, 14 days, K = 20). That queue is alerts only. It is not the GAIA full queue.

| | Baseline (current columns) | `hash-v1` template columns |
|---|---:|---:|
| Held-out faults caught | 4 / 5 (0.8) | 4 / 5 (0.8) |
| False items a day | 0.71 | 1.0 |
| Per-fault catch difference, 95% interval | | mean 0.0, low 0.0, high 0.0 (n = 5) |

Train 945 alerts, test 921 alerts, 5 held-out faults. The interval does not lie above zero, and the false-item rate is higher, so the decision is **drop**. `LLM_TEMPLATE_FEATURES` stays off. A later GAIA run can revisit that; this sample does not justify turning the flag on.

```bash
cd app
python run_classifier_ablation.py --template-features
```

## Deployment and monitoring (Phase 4)

The default image is `app/Dockerfile` (`python:3.12-slim`). CI runs `docker build` on that file. `app/Dockerfile.gpu` is an optional CUDA runtime image (`nvidia/cuda:12.6.3-runtime-ubuntu24.04`) that installs the same hash-pinned CPU lock. It does not install cuML, the `gpu` group, or `llama-cpp-python`. This environment did not pull that base image; the file is checked for the CPU `FROM` line staying on the default Dockerfile, and for the Compose profile naming `Dockerfile.gpu`.

```bash
cd app
docker compose up --build
```

That starts `oncall-queue` only. The GPU service is profile `gpu`. Name it so the CPU service stays down (both publish `127.0.0.1:8080`):

```bash
cd app
docker compose --profile gpu up --build oncall-queue-gpu
```

The same image without Compose:

```bash
cd app
docker build -f Dockerfile.gpu -t oncall-queue:gpu .
docker run --gpus all -p 127.0.0.1:8080:8080 oncall-queue:gpu
```

`/health` keeps `status`, `db_ok`, `shadow_mode`, `login_required`, `pagerduty_configured`, and `channels`. It adds:

| Field | Meaning |
|---|---|
| `accel.backend` | `sklearn`, `cuml`, or `xgboost-gpu`, from `active_backend()` (the same choice as `get_backend`, cached so the probe is not repeated on every check) |
| `llm.enabled` | True only when `LLM_EXPLAIN` is on |
| `llm.weights` | `missing` (no file), `present` (file exists, explanations off, so it is not hashed), `hash-ok`, `hash-mismatch`, `unreadable`, or `error` |
| `llm.last_error` | Last failed note status (`timeout`, `missing`, `hash`, `no-backend`, `error`, …), or null after a clean run |

`/health` does not probe Prometheus and does not query GPU memory. While explanations are off it does not hash the GGUF.

`/metrics` adds `oncall_llm_explain_duration_seconds` (latency), `oncall_llm_explain_total{outcome}`, `oncall_llm_timeout_rate`, `oncall_llm_fallback_rate`, and `oncall_gpu_memory_mebibytes` when `nvidia-smi` reports used memory. Attempts are counted only when explanations are on. `timeout` is a timed-out completion. `fallback` is a note that was not produced because the pin, the file, or `llama-cpp-python` was unusable. An unset flag is not an attempt, so the default process does not show a 100% fallback rate.

`/monitoring` adds check `template_feature_drift`. It is `n/a` while `LLM_TEMPLATE_FEATURES` is off, including when older jobs happen to store a mean. When the flag is on, a job stores the mean of `tpl_prior` (each value counted only from rows at or before that alert) and the check uses the same 0.5–2 band as alert volume. The flag stays off after the Phase 2b drop.

Version stays 1.0.5 until a release that includes this work.
