# MLOps and lifecycle

**System:** AI-Assisted On-Call Queue · **Version:** 1.2.0

This page explains how the AI part of the system is versioned, reproduced, updated and tracked after it is deployed. The AI component is a cost-sensitive logistic regression that scores log alerts (scikit-learn), next to rule-based metric detectors. No external model API is used.

## 1. Versioning

| What | How it is versioned | Where |
|---|---|---|
| **Source code** | Git; every change through a pull request with green CI; releases tagged and listed in `CHANGELOG.md` | GitHub `amamsy90/alert-fatigue-reduction` |
| **Dependencies** | `app/pyproject.toml` (ranges) + `app/uv.lock` (exact, every Python 3.10–3.12) + `app/requirements.lock` (hashes, for Docker); CI fails if the two locks disagree | `app/` |
| **Configuration** | Defaults in `src/config.py` (reviewed like code); per-environment values in `app/deploy/*.env`; settings chosen on *Evaluate my data* saved to `settings.json` with the evaluation id and time they came from | `app/src/config.py`, `app/deploy/`, `product/data/settings.json` |
| **Data** | Public benchmarks (GAIA, NAB, SMD, RCAEval) are downloaded by scripts in `tools/` (GAIA and RCAEval RE2/RE3 with SHA-256 checks), not stored in git; each experiment run records a **fingerprint** of its input files. Each trained model records a fingerprint of its training alerts and labels | `results/experiments.jsonl`, `models/registry.json` |
| **Models** | Every trained model is kept as `models/vNNNN.pkl` (a zip of JSON and a skops file, not a pickle) with a SHA-256 and a **model card** in `models/registry.json`; one version is active (`model.pkl`). A corrupt registry is refused (503); `POST /models/reset` with `confirm=RESET` quarantines it | `product/data/models/` (Docker volume) |
| **Prompts** | Shadow notes use template `explain-v1` (`product/llm.py`). A record that has a note stores `prompt_version` and `llm_version`. Notes are off unless `LLM_EXPLAIN` is set, and they are not an input to the queue. The weight file is pinned by name, revision and SHA-256. `/health` reports whether that file is missing, present, or hash-ok, and `/metrics` records note latency, timeout rate and fallback rate | `product/llm_pin.json`, `scripts/fetch_llm.py`, `product/monitoring.py` |

A model card records: version, training time, label source (real labels, on-call marks), number of training alerts, thresholds, training-data fingerprint, code version (git commit), held-out precision / recall / false positives / misses, the active model's result on the same held-out alerts, and the promotion decision with its reason.

The optional metric model and learned ranker are not versions in that registry. They are separate hash-checked files (`METRIC_MODEL_DIR`, `LEARNED_RANKER_PATH`), off unless those flags are set, and they do not pass the promotion gate because they do not replace the alert classifier. `docs/ML_MODELS.md` is the keep rule for turning them on.

## 2. Reproducibility

If another team member receives the project tomorrow:

```bash
git clone https://github.com/amamsy90/alert-fatigue-reduction && cd alert-fatigue-reduction/app
uv sync --group dev                   # exact versions from uv.lock
uv run pytest -q                      # 523 passed, 10 skipped on CPU CI (7 November 2026); the integration test needs Docker (see test_plan.md)
uv run python run_validate.py         # the report's 259 checked numbers match the committed results
docker compose up -d                  # the CPU app, built from the hash-pinned lock (dev settings)
# optional GPU machine: docker compose --profile gpu up --build oncall-queue-gpu
```

To regenerate the published results they also need the GAIA files (`app/data/gaia_raw`, `app/data/gaia_metrics`), which `python tools/fetch_gaia.py` downloads from https://github.com/CloudWise-OpenSource/GAIA-DataSet and checks by SHA-256 (`FINAL_REPORT.md` §C1), and run the `run_*.py` scripts; `tools/fetch_nab.py`, `tools/fetch_smd.py` and `tools/fetch_rcaeval_mem.py` download the other benchmarks. Every environment variable is listed in `app/README.md`; containers are defined by `app/Dockerfile` and `app/docker-compose.yml`.

## 3. Model and AI updates

| What happens | What the system does |
|---|---|
| **New labelled data arrives** (an upload with past incidents, or Real / Noise marks) | The next batch with two label classes trains a candidate model on its early window and scores it on the later window. |
| **A new model is trained** | It is registered as a new version and must pass the promotion gate on the batch's held-out window: (1) its **held-out cost** (10 × misses + false alarms) is below the cost of showing **no log alerts**; (2) the **whole queue** with its alerts costs less (10 × missed faults + false items) than the same queue **without log alerts** and than the queue the batch **falls back to** if the model is refused (the active model's alerts, or severity rules), by at least **one missed fault** (10; `PROMOTION_MIN_SAVING_MISSES`, default 1), so it is tested against what would actually run and a saving one held-out case could produce alone does not promote it (RCAEval RE2 Online Boutique: 3 false items in 45 days, now refused); (3) if a model is active, its cost is no higher than that model's **on the same held-out alerts**. The first model is not promoted just because none is active. A model that fails stays a candidate and does **not** score the batch it was trained on: the active model does, or rules only (log severity) if there is none. On GAIA the classifier passes (1) and fails (2), so GAIA runs on rules only (`FINAL_REPORT.md` §B4). |
| **The model's performance changes in production** | `/monitoring` tracks precision from Real / Noise marks against a target of 0.7 (with at least 20 marks), alert-volume drift, and monitoring gaps. The Queue page reminds the operator to retrain after 7 days or new Real marks. |
| **A model turns out to be worse** | Roll back: `POST /models/{version}/activate`. The file's SHA-256 is checked before it is used; the change is audited. Numeric retrain and rollback triggers: `FINAL_REPORT.md` §9. |
| **The library or scikit-learn version changes** | A lock-file change is a pull request; CI runs every test, and the experiment scripts are rerun so `run_validate.py` can compare the report's numbers. |
| **Detector settings change** (jump size, window) | Tuned on *Evaluate my data* against the team's past incidents with a held-out part and a chance line; applying them is an explicit action recorded with the evaluation id. |
| **The knowledge base / an external AI API changes** | Not applicable: no knowledge base or external model API. |

**Delivery.** A release is built once, scanned, given an SBOM, deployed to a throwaway cluster with the Helm chart, smoke-tested and only then published; an upgrade that fails its smoke test is rolled back by `tools/deploy.sh`. See `docs/deployment.md` section 3c.

**Scheduled retraining.** `python -m product.cli retrain` (or `POST /models/retrain`) is what a scheduler runs; it does nothing unless a retrain is due. Due means at least 5 new Real marks since the model was trained, or a model older than 7 days with at least one new mark; never twice within 6 hours, and never while a scoring job is running. It trains on the alerts stored by the last 60 jobs, labelled from the on-call marks, through the same promotion gate as every other job. A promoted model is read back from disk through the hash and allowlist checks and must score real alerts into valid probabilities; if it cannot, the previous version is restored automatically (or live jobs fall back to rules only if there was none) and the card records the failed verification. Every card records the trigger, the reasons, the data fingerprint, the code commit, the batch window and the library versions. Example Kubernetes CronJob command: `python -m product.cli retrain` every 6 hours. `/metrics` exports `oncall_retrain_total{outcome}`, `oncall_model_age_days` and `oncall_model_retrain_due`.

**Shadow challenger.** A model the promotion gate refuses is not thrown away. It stays a candidate, and on every later job the newest candidate decides the same live alerts as the active model without changing anything shown or paged. Its cost on labelled alerts (a miss 10, a false item 1) is added to a running tally in its registry card. After 3 jobs and 30 labelled alerts the tally says `promote` only if the challenger saved at least the gate's margin and won more jobs than it lost; otherwise `keep`. A person reads the recommendation and promotes with `POST /models/{version}/activate`, which can be rolled back. This turns one held-out window into a decision on live data, and it is visible at `/monitoring` (`challenger_shadow`) and `/metrics` (`oncall_challenger_*`).

**Testing a new version before release.** Code: CI on every PR (lint, types, the suite on two Python versions, coverage ≥ 85%, pip-audit, report check, Docker health). On 7 November 2026, Python 3.12 collected 533 tests: 523 passed, 10 skipped, coverage 86%. Models: the promotion gate on held-out alerts, and shadow mode (Live is off by default), so a new model can be watched on real traffic before anyone is paged.

## 4. Experiment tracking

Every experiment script appends one line to `app/results/experiments.jsonl` (kept in git):

| Field | Records |
|---|---|
| `at`, `experiment` | When and which script |
| `note` | Why it was run (`EXPERIMENT_NOTE=...`) |
| `code` | Git commit, and whether `src/` or `run_*.py` had uncommitted changes |
| `python`, `libraries` | Python, pandas, NumPy and scikit-learn versions |
| `config` | The full configuration used |
| `data` | Fingerprint of the input files |
| `outputs` | SHA-256 of every result file written |
| `metrics` | Headline numbers (for example detection at K = 20, walk-forward means) |

`git log -p app/results/experiments.jsonl` is therefore the history of every committed result: what changed, why, on which code and data, and what came out. `run_validate.py` then ties the results to the numbers printed in the report (ADR 0006).

A run appends a line. It does not rewrite earlier lines. Do not check `app/results/experiments.jsonl` out, truncate it, or replace it with an older copy to make a diff empty. That drops the rows a laptop or GPU run just added. Keep the run log by committing the appended file, or leave the file uncommitted. `git log -p` shows only lines that were committed. Tests write a temporary log and do not require this file to match HEAD.
