# Test plan

**System:** Budgeted Incident Detection · **Version:** 1.0.0

## 1. Goals

1. Every requirement in `docs/requirements.md` has at least one automated check (see its traceability matrix).
2. No decision uses data from later than the moment it is made (NFR-1).
3. The numbers in `FINAL_REPORT.md` are the numbers the code produces (FR-16).
4. A change that breaks any of the above cannot be merged (CI).

## 2. Test levels

| Level | What it covers | Where | Examples |
|---|---|---|---|
| Unit | One function or rule, synthetic data | `test_memory_spikes.py`, `test_queue.py`, `test_policy.py`, `test_features.py`, `test_metrics.py`, `test_eval_stats.py` | first come beats biggest first; a jump is confirmed at its second point |
| Causality | Adding later data never changes an earlier decision | `test_causality.py`, `test_series_threshold.py`, `test_phase3.py` | early alerts keep their slots when severe ones arrive later |
| Integration (API) | HTTP routes with the real pipeline and a temporary SQLite database, outbound calls mocked | `test_product_api.py`, `test_ops_*.py`, `test_notifiers.py`, `test_page_watermark.py`, `test_would_have_paged.py`, `test_app_hardening.py` | webhook → buffer → job → queue; Live fans out to every channel |
| Security | Access rules, SSRF, secrets, limits | `test_security.py`, `test_production_ready.py` | loopback tricks refused; forwarded request needs a password |
| Reliability | Concurrency and persistence | `test_reliability.py`, `test_app_hardening.py` | concurrent `ops.json` writes keep every change |
| System (experiments) | The whole pipeline on GAIA, NAB, SMD | `run_*.py` scripts → `app/results/` | walk-forward over 8 weeks |
| Acceptance of results | Report matches results | `run_validate.py` | 51 checked numbers plus guarded claims |
| Deployment smoke | The image builds and answers `/health` | CI `docker` job | |

Outbound HTTP (Prometheus, PagerDuty, Slack, Opsgenie) is always mocked in tests; no test needs the network.

## 3. How to run

```bash
cd app
uv sync --group dev              # locked environment with pytest-cov, ruff, mypy
uv run pytest -q --cov           # 186 tests, coverage report
uv run ruff check .
uv run mypy
uv run python run_validate.py    # needs app/results/ (in git)
```

Plain pip also works: `pip install -r requirements.txt pytest && python -m pytest -q` from `app/`.

## 4. CI gates (`.github/workflows/ci.yml`)

| Gate | Rule | Current |
|---|---|---|
| Lock | `requirements.lock` matches `uv.lock` | in sync |
| Lint | `ruff check` clean | clean |
| Types | mypy error count ≤ `MYPY_BUDGET` | 39 of 39 |
| Tests | all pass on Python 3.10 and 3.12 | 186 pass |
| Coverage | line coverage ≥ `COVERAGE_MIN` | 86% (min 85%) |
| Results | `run_validate.py` passes | 51 numbers match |
| Image | `docker build` and `/health` answers | passes locally |

**Type budget policy.** mypy was added to a codebase with 39 existing errors (mostly `Optional` narrowing in `pipeline.py` / `settings.py` and the untyped queue payload). CI fails if the count rises. Any change that removes errors lowers `MYPY_BUDGET` in the same commit, so the number can only go down.

## 5. Coverage by module (line coverage, 186 tests)

| Module | Coverage | Note |
|---|---:|---|
| `product/cli.py`, `model_store.py`, `settings.py` | 95–98% | |
| `product/pipeline.py`, `store.py`, `ops.py` | 90–92% | |
| `product/app.py` | 84% | untested branches are mostly error paths of Evaluate and dataset fetch |
| `src/queue.py`, `policy.py`, `jump_ranker.py`, `classifier.py`, `ingestion.py` | 91–98% | |
| `src/generic.py`, `memory_spikes.py`, `baselines_metric.py` | 86–88% | |
| `src/metrics.py`, `eval_stats.py`, `gaia_loader.py` | 62–75% | GAIA-only paths |
| `src/experiment.py` | 0% | exercised only by the experiment scripts — Phase 2 adds a unit test |
| **Total** | **86%** | |

## 6. Known gaps

- `src/experiment.py` (every GAIA number) has no unit test.
- No load test: ingest throughput and latency are not measured.
- No browser (UI) tests; the static pages are checked only for the controls they must contain.
- Residual risks R1–R8 in `docs/threat_model.md` have no tests because they are not fixed.
