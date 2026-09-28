# MLOps and lifecycle

**System:** Budgeted Incident Detection · **Version:** 1.0.0

This page explains how the AI part of the system is versioned, reproduced, updated and tracked after it is deployed. The AI component is a cost-sensitive logistic regression that scores log alerts (scikit-learn), next to rule-based metric detectors. No external model API is used.

## 1. Versioning

| What | How it is versioned | Where |
|---|---|---|
| **Source code** | Git; every change through a pull request with green CI; releases tagged and listed in `CHANGELOG.md` | GitHub `amamsy90/alert-fatigue-reduction` |
| **Dependencies** | `app/pyproject.toml` (ranges) + `app/uv.lock` (exact, every Python 3.10–3.12) + `app/requirements.lock` (hashes, for Docker); CI fails if the two locks disagree | `app/` |
| **Configuration** | Defaults in `src/config.py` (reviewed like code); per-environment values in `app/deploy/*.env`; settings chosen on *Evaluate my data* saved to `settings.json` with the evaluation id and time they came from | `app/src/config.py`, `app/deploy/`, `product/data/settings.json` |
| **Data** | Public benchmarks (GAIA, NAB, SMD) are downloaded by scripts, not stored in git; each experiment run records a **fingerprint** of its input files. Each trained model records a fingerprint of its training alerts and labels | `results/experiments.jsonl`, `models/registry.json` |
| **Models** | Every trained model is kept as `models/vNNNN.pkl` with a SHA-256 and a **model card** in `models/registry.json`; one version is active (`model.pkl`) | `product/data/models/` (Docker volume) |
| **Prompts** | Not applicable: no LLM is used | — |

A model card records: version, training time, label source (real labels, on-call marks), number of training alerts, thresholds, training-data fingerprint, code version (git commit), held-out precision / recall / false positives / misses, the active model's result on the same held-out alerts, and the promotion decision with its reason.

## 2. Reproducibility

If another team member receives the project tomorrow:

```bash
git clone https://github.com/amamsy90/alert-fatigue-reduction && cd alert-fatigue-reduction/app
uv sync --group dev                   # exact versions from uv.lock
uv run pytest -q                      # 226 tests, no network needed
uv run python run_validate.py         # the report's 51 numbers match the committed results
docker compose up -d                  # the app, built from the hash-pinned lock (dev settings)
```

To regenerate the published results they also need the GAIA files (`app/data/gaia_raw`, `app/data/gaia_metrics`; see `FINAL_REPORT.md` §12) and run the `run_*.py` scripts; `tools/fetch_nab.py` and `tools/fetch_smd.py` download the other benchmarks. Every environment variable is listed in `app/README.md`; containers are defined by `app/Dockerfile` and `app/docker-compose.yml`.

## 3. Model and AI updates

| What happens | What the system does |
|---|---|
| **New labelled data arrives** (an upload with past incidents, or Real / Noise marks) | The next batch with two label classes trains a candidate model on its early window and scores it on the later window. |
| **A new model is trained** | It is registered as a new version. It becomes active only if its **held-out cost** (10 × misses + false alarms) is no higher than the active model's cost **on the same held-out alerts** (the promotion gate). Otherwise it stays a candidate and the active model keeps scoring live batches. |
| **The model's performance changes in production** | `/monitoring` tracks precision from Real / Noise marks against a target of 0.7 (with at least 20 marks), alert-volume drift, and monitoring gaps. The Queue page reminds the operator to retrain after 7 days or new Real marks. |
| **A model turns out to be worse** | Roll back: `POST /models/{version}/activate`. The file's SHA-256 is checked before it is used; the change is audited. |
| **The library or scikit-learn version changes** | A lock-file change is a pull request; CI runs every test, and the experiment scripts are rerun so `run_validate.py` can compare the report's numbers. |
| **Detector settings change** (jump size, window) | Tuned on *Evaluate my data* against the team's past incidents with a held-out part and a chance line; applying them is an explicit action recorded with the evaluation id. |
| **The knowledge base / an external AI API changes** | Not applicable: no knowledge base or external model API. |

**Testing a new version before release.** Code: CI on every PR (lint, types, 226 tests on two Python versions, coverage ≥ 85%, report check, Docker health). Models: the promotion gate on held-out alerts, and shadow mode (Live is off by default), so a new model can be watched on real traffic before anyone is paged.

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

`git log -p app/results/experiments.jsonl` is therefore the history of every result: what changed, why, on which code and data, and what came out. `run_validate.py` then ties the results to the numbers printed in the report (ADR 0006).
