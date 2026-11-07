# AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices

*(Alert Fatigue Reduction in Cloud Microservice Operations)*

**Version:** 1.2.0

An on-call engineer can read only a few alerts a day, and 82% of GAIA faults never write a log line. This system shows at most K items a day, in the order they become known, using memory jumps, slow leaks, and monitoring gaps. At K = 20 the jump detector catches 72.6% of 1,223 test faults, against 68.9% for a Prometheus rate rule and 0% for log severity. The jump detector wins all 8 walk-forward weeks against that rate rule, with fewer false items a day. The report is [`FINAL_REPORT.md`](FINAL_REPORT.md). Optional ranker, metric, trace and LLM paths stay off by default. Their measurements stay in the report appendices.

## Team and contributions

Solo project. **Aishah Mohamed Alyammahi** did all of the work: problem definition, design, implementation (`app/src`, `app/product`), tests, evaluation scripts, CI and Docker, and the reports. AI coding assistants were used for code review, refactoring, test writing and drafting documentation (see `AGENTS.md` and the `claude/*` pull requests); the running system calls no generative AI or external AI service.

## Prerequisites and tested environment

| Item | Value |
|---|---|
| Tested OS | Windows 11 (development, tests, resource measurements); Linux container `ghcr.io/astral-sh/uv:python3.14-bookworm` (clean-clone timing); GitHub Actions runs `ubuntu-latest` on Python 3.12 and one other version |
| Python | 3.12 (development and CI); the project requires `>=3.10,<3.13` (`app/pyproject.toml`) |
| Hardware | A normal laptop or VM; scoring the GAIA month peaked at 505 MB of memory (measured on the author's 20-logical-CPU laptop). Train Ticket RE2 experiments need several GB of RAM. No GPU needed (the GPU and LLM groups are optional) |
| Tools | [uv](https://docs.astral.sh/uv/); Docker (optional, for `docker compose up`); Node only to rebuild the Word copies |
| Paid services | None. No paid API, no external AI service. PagerDuty, Slack and Opsgenie are used only if the operator adds their own credentials |
| Data | Not in the repository (about 420 MB extracted). Fetch scripts under `tools/` pin the source URL and check SHA-256; see "Benchmark data" below |

Copy `app/.env.example` to `app/.env` and fill in the placeholders only if you change settings (the Docker default uses `app/deploy/dev.env`); nothing runs live without `APP_PASSWORD`, a channel and the typed word `LIVE`.

## Installation

```bash
cd app
uv sync --group dev                  # exact versions from uv.lock
```

If `uv sync` fails with a hardlink error (for example in a OneDrive folder on Windows), run it with `UV_LINK_MODE=copy` (PowerShell: `$env:UV_LINK_MODE = "copy"`) or keep the virtual environment outside the synced folder with `UV_PROJECT_ENVIRONMENT`.

## Configuration

Settings are environment variables, documented in [`app/README.md`](app/README.md#settings-environment-variables). Docker uses `app/deploy/dev.env` by default; for production copy `app/deploy/prod.env.example` to `app/deploy/prod.env` (not in git) and run `ENV_FILE=deploy/prod.env docker compose up -d`. `app/.env.example` lists the secrets as empty placeholders.

## Usage

```bash
cd app
docker compose up -d                 # http://127.0.0.1:8080  (shadow mode: nothing is paged)
# or, without Docker:
uv run uvicorn product.app:app --reload
```

Open the queue page and press **Run sample**, or post alerts from Alertmanager. Try it on your own data without code at `/evaluate` ([`USE_YOUR_DATA.md`](USE_YOUR_DATA.md)). The command-line tool is `uv run python -m product.cli --help`.

## Testing

```bash
cd app
uv run pytest -q                     # 531 passed, 10 skipped (9 need torch, xgboost or CUDA; the integration test needs Docker)
uv run ruff check .
uv run mypy
uv run python run_validate.py        # the report's 278 checked numbers match app/results/
```

**Measured:** Windows 11, Python 3.12: `pytest -q` takes about 2 min (541 tests: 531 passed, 10 skipped). A clean clone in a Linux container (`ghcr.io/astral-sh/uv:python3.14-bookworm`, 14 Oct 2026, with an earlier 301-test suite) took 8 min 13 s for `uv sync --group dev` with an empty cache and 58 s for the tests. `docker compose up -d --build` serves `/health` about 30 s after the build finishes.

## Evaluation

**Reproduce in one command (no download; about 10 s warm, 45 s on a first run).** A real 10-day slice of GAIA is committed in [`app/examples/gaia_sample/`](app/examples/gaia_sample) (1.7 MB, untouched logs and memory series; made by `tools/make_gaia_sample.py`). The command trains on its first 7 days, scores the last 3 at K = 10 and 20, and fails unless the caught-fault counts equal the recorded ones in `expected.json` and the jump detector beats the rate rule at K = 20:

```bash
cd app
uv run python run_reproduce.py             # sample: expect "REPRODUCED" (59 test faults, K = 20: jump detector 53, rate rule 46)
uv run python run_reproduce.py --full      # all 62 days: run_queue_eval, run_baseline_compare, then run_validate (needs the GAIA download below)
```

To check that a fresh clone works with nothing carried over from this machine (clone, `uv sync --locked`, reproduce; add `--tests` for the whole suite):

```bash
python tools/check_fresh_clone.py          # or: --source https://github.com/amamsy90/alert-fatigue-reduction --ref <commit>
```

**Measured** (`python tools/check_fresh_clone.py --tests`, Windows 11, Python 3.12.10, commit `bb635c5`, 7 Nov 2026): clone 1 s, `uv sync --locked --group dev` 39 s (warm uv cache), `run_reproduce.py` 45 s, `pytest -q` 110 s (531 passed, 10 skipped), final line `PASS`. A first install with an empty cache takes longer (8 min on the earlier Linux container run above).

Replay real alerts through the whole product loop (drift, marks, scheduled retrain, promotion gate), with a simulated engineer, no download: `uv run python run_live_replay.py` (results in `app/results/live_replay_gaia.json`; `--dataset rcaeval:RE2:tt` needs the RCAEval fetch). A replay is not a pilot; see `FINAL_REPORT.md` section B8.

The sample is a smoke-scale check, not the 1,223-fault result in the report; `--full` reproduces that.

`run_validate.py` and the tests need only the committed `app/results/`. To rerun an experiment, fetch its data first (not in the repository, about 420 MB extracted); each script pins its source URL and checks SHA-256 where the source publishes stable files:

```bash
cd app
uv run python ../tools/fetch_nab.py            # NAB, MIT licence (~15 MB); then: uv run python run_nab.py  (about 1 min in total)
uv run python ../tools/fetch_smd.py            # SMD (OmniAnomaly)
uv run python ../tools/fetch_rcaeval_mem.py    # RCAEval memory cases
uv run python ../tools/fetch_rcaeval_re23.py   # RCAEval RE2/RE3, checked against tools/rcaeval_re23_sha256.txt
uv run python ../tools/fetch_gaia.py           # GAIA, ~1.8 GB download, SHA-256 checked (--check to verify only)
```

The full list of experiment commands is in `FINAL_REPORT.md`, Part C.

Two checks added for the final report (both need the data above):

```bash
cd app
uv run python run_baseline_compare.py     # z-score, EWMA, isolation forest and 95% intervals on GAIA (a few minutes)
uv run python run_rcaeval_gate_split.py   # classifier gate decided on cases that are never scored (RE2 and RE3; several GB of RAM, about 15 minutes)
```

**Home lab (optional, shadow only).** Setup is in [`homelab/README.md`](homelab/README.md) and needs k3d, kubectl, Helm 3, curl and systemd. The day-12 snapshot is in the repository, and `./homelab/scripts/reproduce_day12.sh` reproduces its tables from the frozen data and diffs them against `docs/results/homelab_day12/`.

## Project structure

| Path | What |
|---|---|
| [`FINAL_REPORT.md`](FINAL_REPORT.md) | Main report (sections 1–5) and Appendices A–G |
| [`docs/`](docs/README.md) | Requirements, architecture, ADRs, threat model, test plan, deployment, MLOps, monitoring, privacy |
| [`docs/results/`](docs/results/homelab_day12/README.md) | Frozen GAIA ranker re-run and home-lab day-12 summaries |
| [`app/src/`](app/src) | Detection and evaluation library (no HTTP) |
| [`app/product/`](app/product) | Web app, API, job pipeline, connectors, model registry, monitoring |
| [`app/tests/`](app/tests) | 541 tests (531 pass on CPU CI; 9 skip without torch, xgboost or CUDA; 1 runs against real Prometheus and Alertmanager) |
| [`app/examples/gaia_sample/`](app/examples/gaia_sample) | Real 10-day GAIA slice and the recorded counts used by `run_reproduce.py` |
| [`homelab/`](homelab/README.md) | Single-machine multi-company lab (shadow only) |
| [`tools/`](tools) | Data fetch scripts, the sample builder, the fresh-clone check and the Word report builder |
| `app/run_*.py` | One script per experiment; results in [`app/results/`](app/results), logged in `app/results/experiments.jsonl` |
| [`figures/`](figures) | Detection-versus-K and walk-forward plots, from `cd app && uv run python make_figures.py` |
| [`app/deploy/`](app/deploy) | Settings for development, test and production |
| [`app/perf/`](app/perf) | Load test |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |
| [`submission/`](submission/README.md) | The proposal as submitted, and how to rebuild the Word copies (not tracked) |
| `design_specification.md`, `project_plan.md`, `ROADMAP.md`, `VALIDATION.md`, `CODE_REVIEW.md` | Design detail, plan, roadmap, result files, the first review |
| [`report/`](report) | The four-page final report (LaTeX source and PDF) |
| [`presentation/`](presentation) | Demo script, viva answers, speaker notes |
| [`submission/Project_Proposal_200010549_Aishah_Alyammahi.pdf`](submission/Project_Proposal_200010549_Aishah_Alyammahi.pdf) | The proposal as submitted (5 October 2026), with its Word source |
| [`docs/history/`](docs/history) | Earlier proposal drafts in Markdown, and a pilot offer |

## Safety defaults

Shadow mode is on. See [`docs/deployment.md`](docs/deployment.md) and [`docs/threat_model.md`](docs/threat_model.md).
