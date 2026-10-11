# AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices

*(Alert Fatigue Reduction in Cloud Microservice Operations)*

An on-call engineer can read only a few alerts a day, and most real faults never write a log line. This system shows each engineer **at most K items a day, in the order a live system learns about them**. It combines rule-based metric detectors (memory jumps, slow leaks, monitoring gaps) with a cost-sensitive classifier for log alerts, pages safely, learns from Real / Noise marks, and ships as one Docker container.

[![CI](https://github.com/amamsy90/alert-fatigue-reduction/actions/workflows/ci.yml/badge.svg)](https://github.com/amamsy90/alert-fatigue-reduction/actions/workflows/ci.yml)

On the GAIA microservice benchmark at 20 items a day, the detector catches **72.6%** of 1,223 test faults (the best possible is 84.9%), against 68.9% for a Prometheus rate rule and 0% for log severity. It wins all 8 walk-forward weeks with fewer false items. Details and limits are in [`FINAL_REPORT.md`](FINAL_REPORT.md).

## Start

```bash
cd app
docker compose up -d                 # http://127.0.0.1:8080  (shadow mode: nothing is paged)
```

For development:

```bash
cd app
uv sync --group dev                  # exact versions from uv.lock
uv run pytest -q                     # 300 tests (the 301st, integration, needs Docker)
uv run python run_validate.py        # the report's 195 checked numbers match app/results/
uv run uvicorn product.app:app --reload
```

Try it on your own data without code: open `/evaluate` ([`USE_YOUR_DATA.md`](USE_YOUR_DATA.md)).

## What is where

| Path | What |
|---|---|
| [`FINAL_REPORT.md`](FINAL_REPORT.md) | The report: Part A follows the course brief's 13 sections (with a rubric map), Part B the results, Part C how to reproduce them |
| [`docs/`](docs/README.md) | Requirements and traceability, architecture diagrams, decision records, threat model, test plan, deployment and AI governance, MLOps, monitoring, privacy and responsible AI, retrospective |
| [`app/src/`](app/src) | Detection and evaluation library (no HTTP) |
| [`app/product/`](app/product) | Web app, API, job pipeline, connectors, model registry, monitoring |
| [`app/tests/`](app/tests) | 301 automated tests (one runs against real Prometheus and Alertmanager) |
| `app/run_*.py` | One script per experiment; results in [`app/results/`](app/results), logged in `app/results/experiments.jsonl` |
| [`figures/`](figures) | Detection-versus-K and walk-forward plots, from `cd app && uv run python make_figures.py` |
| [`app/deploy/`](app/deploy) | Settings for development, test and production |
| [`app/perf/`](app/perf) | Load test |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |
| [`submission/`](submission/README.md) | Word / PDF copies for upload (regenerate from the Markdown first) |
| `design_specification.md`, `project_plan.md`, `ROADMAP.md`, `VALIDATION.md`, `CODE_REVIEW.md` | Design detail, plan, roadmap, result files, the first review |
| `DEMO_SCRIPT.md`, `VIVA_ANSWERS.md`, `Alert_Fatigue_Speaker_Notes.md` | Presentation and viva material |
| [`submission/Project_Proposal_200010549_Aishah_Alyammahi.pdf`](submission/Project_Proposal_200010549_Aishah_Alyammahi.pdf) | The proposal as submitted (5 October 2026), with its Word source |
| `PROJECT_PROPOSAL.md`, `proposal.md`, `UAE_PILOT_OFFER.md` | Earlier proposal drafts in Markdown, and a pilot offer |

## Safety defaults

Shadow mode is on: every job records what it *would* have paged. Live paging needs a login password, at least one channel, and `LIVE` typed on the Connectors page, and only webhook-ingest jobs page. See [`docs/deployment.md`](docs/deployment.md) and [`docs/threat_model.md`](docs/threat_model.md).
