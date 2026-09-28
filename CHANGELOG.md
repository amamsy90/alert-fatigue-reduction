# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/). Dates are commit dates.

## [Unreleased] — toward 1.0.0

### Changed (Phase 2 refactor — no result changed; GAIA and NAB reruns are identical)
- `product/app.py` split into `product/web/` (security, schemas, uploads, context, three route modules).
- `product/pipeline.py` split into `inputs.py`, `labels.py`, `paging.py`; `run_job` is a short orchestrator.
- One definition of a catch and one threshold search in `src/scoring.py`, replacing four hand-written overlap checks and two copies of the tuning loop.
- `QueueItem.payload` is typed; mypy 39 errors → 0 (CI budget 0).
- Alerts-only milestone scripts moved to `app/experiments/legacy_alerts_only/`; deck builder to `tools/build_rehearsal.py`.
- Tests for `experiment.py`, `scoring.py` and `settings.py` (186 → 200 tests, coverage 86% → 89%).

### Added
- Requirements with IDs, user stories and a traceability matrix (`docs/requirements.md`).
- Architecture diagrams: context, components, ingest-to-page sequence, deployment, data model (`docs/architecture.md`).
- Decision records (`docs/adr/`), threat model (`docs/threat_model.md`), test plan (`docs/test_plan.md`), retrospective (`docs/retrospective.md`).
- `app/pyproject.toml`, `app/uv.lock` and a hash-pinned `app/requirements.lock`; the Docker image installs the lock.
- CI: lock check, ruff, a mypy error budget, tests on Python 3.10 and 3.12 with coverage ≥ 85%, `run_validate.py`, Docker build and health check.
- `FINAL_REPORT.md` *Checked numbers* table; `run_validate.py` reads it instead of numbers typed into its own source.
- Audit log for changes to Live mode, keys, the Prometheus URL and settings.
- Ingest de-duplication (`INGEST_DEDUP_HOURS`), `ALLOWED_HOSTS`, `MAX_INGEST_MB`.
- Tests: `test_security.py`, `test_reliability.py`, `test_causality.py`, `test_run_validate.py` (150 → 186 tests).

### Changed
- The queue has no look-ahead left: alert filters are first come, first served; a leak is dropped only by a jump already shown; the robust spread uses earlier points only; an alert rides on a jump only while `Spike.open_at` says it is open; a dead feed is reported before data returns.
- Results after that change: full queue at K = 20 70.2% → 69.6%; walk-forward 72.5% → 71.3%, now tied with the rate rule week by week; the detector alone unchanged at 72.6% (8/8 weeks). SMD at K = 20: 55.1% of incidents.
- Live paging needs `APP_PASSWORD`, checked on the switch and in `can_page()`.
- Docker and compose default to `PROM_ALLOW_PRIVATE=0`.
- Page marks are kept per channel, so a retry re-sends only to the channel that failed.

### Fixed
- SSRF: numeric and dotted forms of loopback, and public names that resolve to private or metadata addresses, passed the Prometheus check; redirects were followed.
- A request through a reverse proxy was treated as private and served without a password.
- DNS rebinding and cross-site requests could drive the local API.
- The ingest token was accepted in the URL.
- Large uploads reached temp disk before the size check; `/ingest/*` had no body limit.
- SQLite connections were never closed; concurrent `ops.json` writes could lose a change.
- A retry after 409 buffered, and could page, the same alerts twice; `/evaluate` could run in parallel and left folders behind on errors.

## [0.4.0] — 2026-09-27 — Connectors and pilot hardening
### Added
- Live PagerDuty with a typed `LIVE` switch and demo guards; Slack and Opsgenie with the same item list; *would-have-paged* in shadow mode.
- Prometheus test pull from the Connectors page; per-series jump size; SMD benchmark; demo dry-run; UAE pilot offer.
- One job at a time (409), job and evaluation retention, upload cap, input checks, cheap `/health`, first SSRF gate, Docker image.
### Fixed
- Page only metric items new since the last ingest job; hide Prometheus URL credentials.
- A jump at the same instant as an alert keeps its slot; misses are scored against the full queue at the largest K.

## [0.3.0] — 2026-09-26 — Causal queue and fair baselines
### Added
- Causal, first-come daily queue; gap items; slow-leak rule; Prometheus-style rate and static baselines; oracle ceiling.
- *Evaluate my data* in the app and CLI for any metric table; per-series confirm levels; demo script.
### Changed
- Hindsight ranking by final peak replaced by a confirmation threshold learned on week 1.
- Superseded experiment scripts removed; every document refreshed after the first code review (`CODE_REVIEW.md`).

## [0.2.0] — 2026-09-24 — Honest statistics
### Changed
- The 95% detection target replaced by walk-forward statistics with a sign test and bootstrap interval.
### Fixed
- Fold-7 explanation (a monitoring outage) and the sign-test p-value.
### Added
- Prometheus reachability on the Connectors dashboard.

## [0.1.0] — 2026-09-23 — First version
### Added
- GAIA loader with China-to-UTC clock alignment, alert classifier, memory-jump detector, jump-first policy, local on-call web app.
