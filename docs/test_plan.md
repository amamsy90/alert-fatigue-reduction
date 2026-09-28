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
| Unit | One function or rule, synthetic data | `test_memory_spikes.py`, `test_queue.py`, `test_policy.py`, `test_scoring.py`, `test_settings.py`, `test_features.py`, `test_metrics.py`, `test_eval_stats.py` | first come beats biggest first; a jump is confirmed at its second point |
| Causality | Adding later data never changes an earlier decision | `test_causality.py`, `test_series_threshold.py`, `test_phase3.py` | early alerts keep their slots when severe ones arrive later |
| Integration (API) | HTTP routes with the real pipeline and a temporary SQLite database, outbound calls mocked | `test_product_api.py`, `test_ops_*.py`, `test_notifiers.py`, `test_page_watermark.py`, `test_would_have_paged.py`, `test_app_hardening.py` | webhook → buffer → job → queue; Live fans out to every channel |
| Security | Access rules, SSRF, secrets, limits | `test_security.py`, `test_production_ready.py` | loopback tricks refused; forwarded request needs a password |
| AI-specific | Unexpected and malformed input, edge cases, one-class labels, model promotion and rollback | `test_ai_inputs.py`, `test_model_registry.py`, `test_monitoring.py` | a bad webhook row is rejected instead of blocking every later job; a worse model is not activated |
| Performance | Latency, throughput and errors under concurrent users | `perf/load_test.py` against a live server | 50 users: 228 req/s, p95 0.32 s, 0 errors |
| Reliability | Concurrency and persistence | `test_reliability.py`, `test_app_hardening.py` | concurrent `ops.json` writes keep every change |
| System (experiments) | The whole pipeline on GAIA, NAB, SMD; `experiment.py` also on a synthetic 3-day run | `run_*.py` scripts → `app/results/`; `test_experiment.py` | walk-forward over 8 weeks; later data changes nothing |
| Acceptance of results | Report matches results | `run_validate.py` | 51 checked numbers plus guarded claims |
| Deployment smoke | The image builds and answers `/health` | CI `docker` job | |

Outbound HTTP (Prometheus, PagerDuty, Slack, Opsgenie) is always mocked in tests; no test needs the network.

## 3. How to run

```bash
cd app
uv sync --group dev              # locked environment with pytest-cov, ruff, mypy
uv run pytest -q --cov           # 226 tests, coverage report
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
| Types | mypy error count ≤ `MYPY_BUDGET` | 0 of 0 |
| Tests | all pass on Python 3.10 and 3.12 | 226 pass |
| Coverage | line coverage ≥ `COVERAGE_MIN` | 90% (min 85%) |
| Results | `run_validate.py` passes | 51 numbers match |
| Image | `docker build` and `/health` answers | passes locally |

**Type budget policy.** mypy was added to a codebase with 39 existing errors (mostly `Optional` narrowing in `pipeline.py` / `settings.py` and the untyped queue payload). Phase 2 fixed all of them, so `MYPY_BUDGET` is now 0: any new type error fails CI.

## 5. Coverage by module (line coverage at the end of Phase 2, 200 tests; 90% overall with 226)

| Module | Coverage | Note |
|---|---:|---|
| `product/app.py`, `pipeline.py`, `paging.py`, `cli.py`, `model_store.py`, `settings.py` | 95–98% | |
| `product/labels.py`, `store.py`, `ops.py`, `web/security.py`, `web/uploads.py` | 90–93% | |
| `product/inputs.py`, `web/evaluate_routes.py`, `web/job_routes.py`, `web/ops_routes.py` | 76–84% | untested branches are mostly error paths (GAIA folder missing, channel test pages, dataset download) |
| `src/queue.py`, `experiment.py`, `policy.py`, `jump_ranker.py`, `classifier.py`, `ingestion.py`, `baselines_metric.py`, `scoring.py` | 93–100% | |
| `src/generic.py`, `memory_spikes.py` | 87% | |
| `src/metrics.py`, `eval_stats.py`, `gaia_loader.py` | 62–75% | GAIA-only paths and alerts-only metrics |
| **Total** | **89%** | |

## 6. Measurable test criteria

| Criterion | Target | Result |
|---|---|---|
| Automated tests passing | 100% | 226 / 226 on Python 3.10 and 3.12 |
| Line coverage | ≥ 85% | 90% |
| Type errors (mypy) | 0 | 0 |
| p95 latency, 20 / 50 concurrent users | ≤ 1 s | 0.23 s / 0.32 s |
| Throughput | ≥ 20 req/s | 167 / 228 req/s |
| Error rate under load | ≤ 1% | 0% (3,000 requests) |
| Scoring job, 400-alert webhook batch | ≤ 5 s | 0.2 s |
| Full GAIA month (34,863 alerts, with training) | ≤ 5 min | 27 s |
| Report numbers match results | all | 51 / 51 (`run_validate.py`) |
| Malformed webhook rows | never block a job | rejected with a reason (`test_ai_inputs.py`) |
| Critical security issues | 0 | 0 (`docs/threat_model.md`) |

Performance numbers: one container on a laptop (20 logical CPUs), `perf/load_test.py`, mixed traffic (ingest, queue, connectors, health, metrics).

## 6. Known gaps

- The load test runs against a live server by hand (`perf/load_test.py`), not in CI; results are kept in `app/results/perf/`.
- No browser (UI) tests; the static pages are checked only for the controls they must contain.
- Residual risks R1–R8 in `docs/threat_model.md` have no tests because they are not fixed.
