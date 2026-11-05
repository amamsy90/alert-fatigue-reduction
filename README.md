# AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices

*(Alert Fatigue Reduction in Cloud Microservice Operations)*

**Version:** 1.2.0

An on-call engineer can read only a few alerts a day, and 82% of GAIA faults never write a log line. This system shows at most K items a day, in the order they become known, using memory jumps, slow leaks, and monitoring gaps. At K = 20 the jump detector catches 72.6% of 1,223 test faults, against 68.9% for a Prometheus rate rule and 0% for log severity, and it wins all 8 walk-forward weeks with fewer false items. The report is [`FINAL_REPORT.md`](FINAL_REPORT.md). Optional ranker, metric, trace and LLM paths stay off by default. Their measurements stay in the report appendices.

## Team

Aishah Mohamed Alyammahi, solo, responsible for the design, implementation, evaluation, and report.

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

- Python 3.10, 3.11 or 3.12 (`requires-python` is `>=3.10,<3.13` in `app/pyproject.toml`).
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

Published GAIA timing was measured on a Windows laptop (20 logical CPUs, Python 3.12.14; `app/results/resources_gaia.json`). Clean-clone timing used the uv Python 3.14 Bookworm image. No paid API is required. PagerDuty, Slack and Opsgenie are optional and unused in shadow mode.

Tests and `run_validate.py` read the committed files in `app/results/`. GAIA is not in git. `tools/fetch_gaia.py` downloads it (about 1.8 GB) and checks SHA-256.

```bash
cd app
uv sync --group dev
uv run pytest -q
uv run python run_validate.py
```

On this commit, `uv run pytest -q` reported 450 passed and 10 skipped. Line coverage is 86%. `uv run python run_validate.py` matched 195 checked numbers. Exact steps for a grader are these four commands. The same four commands are in section 4 of [`FINAL_REPORT.md`](FINAL_REPORT.md).

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

Home-lab setup is in [`homelab/README.md`](homelab/README.md). The day-12 snapshot is in the repository. Reproduce the day-12 tables with:

```bash
./homelab/scripts/reproduce_day12.sh
```

That checks SHA-256 `378dc71f3ef4143662acc1d0a3dda8c5e32f7752f3ceb77efe9b88bf0985c56e`, scores plain and learned-ranker modes, and diffs the tables in `docs/results/homelab_day12/`. The rebuild has 1,184 episodes; the scored alert file has 1,182 after the metric-window cutoff. Models are trained locally. No paid API is required.

## Safety defaults

Shadow mode is on. See [`docs/deployment.md`](docs/deployment.md) and [`docs/threat_model.md`](docs/threat_model.md).
