# AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices

*(Alert Fatigue Reduction in Cloud Microservice Operations)*

**Version:** 1.2.4

An on-call engineer can read only a few alerts a day, and 82% of GAIA faults never write a log line. This system shows at most K items a day, in the order they become known, using memory jumps, slow leaks, and monitoring gaps. At K = 20 the jump detector catches 72.6% of 1,223 test faults, against 68.9% for a Prometheus rate rule and 0% for log severity. The jump detector wins all 8 walk-forward weeks against that rate rule, with fewer false items a day. The report is [`FINAL_REPORT.md`](FINAL_REPORT.md). Optional ranker, metric, trace and LLM paths stay off by default. Their measurements stay in the report appendices.

## Team and contributions

Solo project. **Aishah Mohamed Alyammahi** did all of the work: problem definition, design, implementation (`app/src`, `app/product`), tests, evaluation scripts, CI and Docker, and the reports. AI coding assistants were used for code review, refactoring, test writing and drafting documentation (see `AGENTS.md` and the `claude/*` pull requests); the running system calls no generative AI or external AI service.

## Prerequisites and tested environment

| Item | Value |
|---|---|
| Tested OS | Windows 11 with Python 3.12 (development, tests, resource measurements). The app container is `python:3.12-slim` (`app/Dockerfile`). GitHub Actions runs Python 3.10 and 3.12 on `ubuntu-latest`, plus a Windows job on Python 3.12 |
| Python | 3.12 (development and CI); the project requires `>=3.10,<3.13` (`app/pyproject.toml`) |
| Hardware | A normal laptop or VM; scoring the GAIA month peaked at 505 MB of memory (measured on the author's 20-logical-CPU laptop). Train Ticket RE2 experiments need several GB of RAM. No GPU needed (the GPU and LLM groups are optional) |
| Tools | [uv](https://docs.astral.sh/uv/); Docker (optional, for `docker compose up`); Node only to rebuild the Word copies |
| Paid services | None. No paid API, no external AI service. PagerDuty, Slack and Opsgenie are used only if the operator adds their own credentials |
| Data | Not in the repository. GAIA is commit `238a9e24ae0b69e7e05dff112652a3fa62db88fd` of CloudWise-OpenSource/GAIA-DataSet (2022-05-12): a 1.79 GB compressed metric archive, of which this project keeps 54.7 MB (two run tables and 37 memory series). See "Benchmark data" below |

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
uv run pytest -q --cov --cov-report=term --cov-fail-under=85   # 530 passed, 22 skipped; line coverage 86.93%
uv run ruff check .
uv run mypy
uv run python run_validate.py        # the report's 300 checked numbers match app/results/
```

**Measured** on this tree, from `app/`, with no Helm installed: `uv run pytest -q --cov --cov-report=term --cov-fail-under=85` reports 530 passed and 22 skipped, line coverage 86.93%. The same 552 tests were 542 passed and 10 skipped on 7 Nov 2026 when Helm was available, because the 12 chart tests in `tests/test_helm_chart.py` then ran. The older 450 passed and 10 skipped figure is the 1.2.0 suite, before those later tests existed. A Windows 11, Python 3.12 `pytest -q` takes about 2 min. `docker compose up -d --build` uses `python:3.12-slim` and serves `/health` about 30 s after the build finishes.

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

**Measured** (`python tools/check_fresh_clone.py --tests`, Windows 11, Python 3.12.10, commit `b8a3cf2`, 7 Nov 2026): clone 1 s, `uv sync --locked --group dev` 39 s (warm uv cache), `run_reproduce.py` 45 s, `pytest -q` 110 s (542 passed, 10 skipped, Helm present), final line `PASS`. A first install with an empty cache takes longer. The current command, without Helm, is the 530 passed and 22 skipped figure above.

Replay real alerts through the whole product loop (drift, marks, scheduled retrain, promotion gate), with a simulated engineer, no download: `uv run python run_live_replay.py` (results in `app/results/live_replay_gaia.json`; `--dataset rcaeval:RE2:tt` needs the RCAEval fetch). A replay is not a pilot; see `docs/report_appendices_full.md` §B8.

Run the live soak (needs Docker, about 2 hours): the app, a real Prometheus and a real Alertmanager around a simulated fleet with faults injected in real time, a simulated engineer marking the queue, and scheduled retrains. `uv run python run_live_soak.py --minutes 120 --warmup 900` writes `app/results/live_soak.json`; add `--drift-phase` (or `--minutes 0 --drift-phase`) to post alert storms that exercise the drift check. It is a soak test, not production traffic and not a pilot; see `docs/report_appendices_full.md` §B9.

The sample is a smoke-scale check, not the 1,223-fault result in the report; `--full` reproduces that.

`run_validate.py` and the tests need only the committed `app/results/`. To rerun an experiment, fetch its data first (not in the repository). GAIA's compressed metric archive is 1.79 GB and the memory-and-run subset kept here is 54.7 MB. NAB is about 15 MB, SMD about 240 MB, and RCAEval RE2/RE3 about 570 MB. Each script pins its source and checks SHA-256 where the source publishes stable files:

```bash
cd app
uv run python ../tools/fetch_nab.py            # NAB, MIT licence (~15 MB); then: uv run python run_nab.py  (about 1 min in total)
uv run python ../tools/fetch_smd.py            # SMD (OmniAnomaly)
uv run python ../tools/fetch_rcaeval_mem.py    # RCAEval memory cases
uv run python ../tools/fetch_rcaeval_re23.py   # RCAEval RE2/RE3, checked against tools/rcaeval_re23_sha256.txt
uv run python ../tools/fetch_gaia.py           # GAIA commit 238a9e24, 1.79 GB download, 54.7 MB kept, SHA-256 checked (--check to verify only)
```

The full list of experiment commands is in `docs/report_appendices_full.md`, Appendix G (C1).

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
| [`FINAL_REPORT.md`](FINAL_REPORT.md) | Submitted report: sections 1–5, Appendix A (GAIA headline tables) and Appendix B (home-lab leave-one-company-out). The upload is [`submission/Alert_Fatigue_Final_Report_v1.2.0.pdf`](submission/Alert_Fatigue_Final_Report_v1.2.0.pdf) |
| [`skills/`](skills/README.md) | Step-by-step instructions for coding agents and contributors: check a change, add a regression test, review and simplify, reproduce and validate, run the live soak, cut a release |
| [`docs/`](docs/README.md) | Requirements, architecture, ADRs, threat model, test plan, deployment, MLOps, monitoring, privacy |
| [`docs/results/`](docs/results/homelab_day12/README.md) | Frozen GAIA ranker re-run and home-lab day-12 summaries |
| [`app/src/`](app/src) | Detection and evaluation library (no HTTP) |
| [`app/product/`](app/product) | Web app, API, job pipeline, connectors, model registry, monitoring |
| [`app/tests/`](app/tests) | 552 tests. Without Helm: 530 passed, 22 skipped (`uv run pytest -q --cov --cov-report=term --cov-fail-under=85`; coverage 86.93%). Twelve skips need Helm, 9 need torch, xgboost or CUDA, and 1 needs the Docker stack |
| [`app/examples/gaia_sample/`](app/examples/gaia_sample) | Real 10-day GAIA slice and the recorded counts used by `run_reproduce.py` |
| [`homelab/`](homelab/README.md) | Single-machine multi-company lab (shadow only) |
| [`tools/`](tools) | Data fetch scripts, the sample builder, the fresh-clone check and the Word report builder |
| `app/run_*.py` | One script per experiment; results in [`app/results/`](app/results), logged in `app/results/experiments.jsonl` |
| [`figures/`](figures) | Detection-versus-K and walk-forward plots, from `cd app && uv run python make_figures.py` |
| [`app/deploy/`](app/deploy) | Settings for development, test and production |
| [`app/perf/`](app/perf) | Load test |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |
| [`submission/`](submission/README.md) | The proposal as submitted, and how to rebuild the Word copies (not tracked) |
| `design_specification.md`, `VALIDATION.md` | Design detail, result files |
| [`docs/report_appendices_full.md`](docs/report_appendices_full.md) | Full appendices (requirements through optional checks) and the 300 checked numbers. Section ids such as §B8 and §C1 live here |
| [`report/`](report/README.md) | Superseded LaTeX report. Graders use the PDF in `submission/` |
| [`presentation/`](presentation) | Demo script, viva answers, speaker notes |
| [`submission/Project_Proposal_200010549_Aishah_Alyammahi.pdf`](submission/Project_Proposal_200010549_Aishah_Alyammahi.pdf) | The proposal as submitted (5 October 2026), with its Word source |
| [`docs/history/`](docs/history) | Earlier proposal drafts in Markdown, and a pilot offer |

## Safety defaults

Shadow mode is on. See [`docs/deployment.md`](docs/deployment.md) and [`docs/threat_model.md`](docs/threat_model.md).
