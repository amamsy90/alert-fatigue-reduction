# AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices

*(Alert Fatigue Reduction in Cloud Microservice Operations)*

An on-call engineer can read only a few alerts a day, and most real faults never write a log line. This system shows each engineer **at most K items a day, in the order a live system learns about them**. It combines rule-based metric detectors (memory jumps, slow leaks, monitoring gaps) with a cost-sensitive classifier for log alerts. An optional learned ranker stays off. The app ships as one Docker container and pages nothing unless Live is armed.

The report is [`FINAL_REPORT.md`](FINAL_REPORT.md). On GAIA at K = 20 the learned ranker catches 887 of 1,223 faults against a baseline of 851. The detector alone catches 72.6%. Those are different rows (`docs/results/gaia_ranker_rerun/summary_by_k.csv` and `jumps_tuned` in `app/results/queue_eval_gaia.csv`).

## Team

Aishah Mohamed Alyammahi is the sole member: requirements, design, implementation, evaluation, the home lab, and documentation. AI coding assistants were used for review, tests and drafting. The author is responsible for the design, the method and the claims.

## Project structure

| Path | What |
|---|---|
| [`FINAL_REPORT.md`](FINAL_REPORT.md) | Main report (sections 1–5) and Appendices A–G |
| [`docs/`](docs/README.md) | Requirements, architecture, ADRs, threat model, test plan, deployment, MLOps, monitoring, privacy |
| [`docs/results/`](docs/results/homelab_day12/README.md) | Frozen GAIA ranker re-run and home-lab day-12 summaries |
| [`app/src/`](app/src) | Detection and evaluation library (no HTTP) |
| [`app/product/`](app/product) | Web app, API, job pipeline, connectors, model registry, monitoring |
| [`app/tests/`](app/tests) | Application tests |
| [`homelab/`](homelab/README.md) | Single-machine multi-company lab (shadow only) |
| `app/run_*.py` | One script per experiment; results in [`app/results/`](app/results) |
| [`figures/`](figures) | Plots and the architecture diagram |
| [`app/deploy/`](app/deploy) | Settings templates for development, test and production |
| [`tools/`](tools) | Data fetch scripts and the report builder |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |

## Prerequisites

- Python 3.10, 3.11 or 3.12 (`requires-python` is `>=3.10,<3.13` in `app/pyproject.toml`). This tree was re-tested on CPython 3.12.3.
- [uv](https://docs.astral.sh/uv/) for the locked environment.
- Docker, if you run the container or the integration test.
- The home lab also needs k3d, kubectl, Helm 3, curl and systemd. See [`homelab/README.md`](homelab/README.md).
- Node.js and `npm ci` only if you regenerate the Word report (`tools/md2docx.js`).

No paid API is required. PagerDuty, Slack and Opsgenie accounts are optional, and only for Live paging, which is off by default. There is no OpenAI or other hosted-model key. Optional GPU packages (PyTorch, XGBoost) are for ablations only.

## Installation

```bash
cd app
uv sync --group dev
```

Or the container, which stays in shadow mode:

```bash
cd app
docker compose up -d
```

Open http://127.0.0.1:8080.

## Configuration

Copy a template and fill the placeholders. Do not commit `.env` or `deploy/prod.env`.

```bash
cp app/.env.example app/.env
```

`app/.env.example` leaves `APP_PASSWORD` and `INGEST_TOKEN` empty. `app/deploy/prod.env.example` is the production template. `app/deploy/dev.env` is the laptop file. `app/deploy/test.env` uses the obvious placeholder `CHANGE_ME_TEST_ONLY` and never pages. The variable list is in [`app/README.md`](app/README.md). To point Compose at a file:

```bash
cd app
ENV_FILE=deploy/prod.env docker compose up -d
```

## Usage

Shadow mode records what would have been paged. Live paging needs `APP_PASSWORD`, a channel, and the typed word `LIVE`, and only ingest jobs page.

```bash
cd app
uv run uvicorn product.app:app --reload
uv run python -m product.cli score --alerts examples/alerts.csv --metrics examples/metrics.csv
```

Own data, without code changes: open `/evaluate` ([`USE_YOUR_DATA.md`](USE_YOUR_DATA.md)).

## Testing and evaluation

Tested on Linux with CPython 3.12.3. GitHub Actions also runs Python 3.10 and 3.12 on `ubuntu-latest`. On this commit:

```bash
cd app
uv run pytest -q                      # 440 passed, 10 skipped
uv run ruff check .                   # clean
uv run python run_validate.py         # 195 checked numbers
uv run --with pyyaml pytest -q ../homelab/tests   # 62 passed
```

The integration job needs Docker (`app/tests/integration`, started from `.github/workflows/ci.yml`).

CPU is enough for the app, the tests and the published GAIA tables. About 20 GB of free RAM is the estimate for the full home lab. An NVIDIA GPU is optional and was used only for the ranker re-run (RTX 5090 Laptop, torch 2.14.1+cu130, xgboost 3.4.1; `docs/results/gaia_ranker_rerun/env.txt`).

Fetch benchmark data, then score it. Each fetch script names its source. GAIA and RCAEval RE2/RE3 check SHA-256.

```bash
cd app
uv run python ../tools/fetch_gaia.py
uv run python ../tools/fetch_nab.py
uv run python ../tools/fetch_smd.py
uv run python ../tools/fetch_rcaeval_mem.py
uv run python ../tools/fetch_rcaeval_re23.py
uv run python run_queue_eval.py
uv run python run_validate.py
```

The GAIA ranker command is in [`docs/results/gaia_ranker_rerun/commands.sh`](docs/results/gaia_ranker_rerun/commands.sh). Home-lab setup is in [`homelab/README.md`](homelab/README.md). The day-12 snapshot is in the repository. Reproduce Table 2 with:

```bash
./homelab/scripts/reproduce_day12.sh
```

That checks SHA-256 `378dc71f3ef4143662acc1d0a3dda8c5e32f7752f3ceb77efe9b88bf0985c56e`, scores plain and learned-ranker modes, and diffs the tables in `docs/results/homelab_day12/`. The rebuild has 1,184 episodes; the scored alert file has 1,182 after the metric-window cutoff. Models are trained locally. No paid API is required.

## Safety defaults

Shadow mode is on. See [`docs/deployment.md`](docs/deployment.md) and [`docs/threat_model.md`](docs/threat_model.md).
