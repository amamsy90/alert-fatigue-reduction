# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/). Dates are commit dates.

## [1.0.1] — 2026-10-11

### Changed (release)
- Docs and report: 289 tests, the coverage table (89.8%), and the model-retention row for `MODEL_CANDIDATE_KEEP`; the report's Word and PDF copies are regenerated.

### Fixed (code review, 11 October 2026, fourth round)
- Paging keys: `PAGE_KEY_MAX` dropped keys in alphabetical order (all `gap:` keys before any `jump:` key), so once the list was full an item still in the history lost its key and was paged again on every job. Keys of items still shown are now never dropped; the rest go oldest first by start time. Test: `test_an_item_still_in_history_is_never_paged_twice_once_the_key_cap_is_reached`.
- Backup copied the live WAL-mode `queue.db` as a plain file, which could tear during a job and missed commits still in the write-ahead log. It now uses SQLite's backup API, and restore deletes the lost database's `-wal` and `-shm` so they are not replayed. Tests in `test_backup.py`.
- `LOG_FORMAT=json` dropped tracebacks; the JSON line now has `exc` and `stack`. Test: `test_logging_config.py`.
- `recent_jobs`, `latest_job_id` and job retention ordered by `created_at` only, so two jobs saved in the same clock tick came back in either order and `/monitoring` could read an older job as the last one. Ties now break on insertion order. Test: `test_jobs_saved_in_the_same_clock_tick_keep_their_order`.
- The release workflow published an image without running any check. It now runs every CI job (`ci.yml` as a reusable workflow) on the tagged commit first.

### Changed (code review, 11 October 2026, fourth round)
- Metric drift on `/monitoring` is per series: the median of each series' last hour against its own earlier jobs, with the drifted series listed (`data.metric_drift_series`). The fleet-wide median over the whole re-scored history barely moved and hid a single service. Jobs saved before this fall back to the old check. Tests in `test_monitoring.py`.
- Refused candidate models: only the newest `MODEL_CANDIDATE_KEEP` (default 20) keep their file; the rest are deleted and their cards marked `file_removed`. Versions that were ever active are never deleted. Test: `test_only_the_newest_refused_models_keep_their_files`.

## [1.0.0] — 2026-10-11

### Added (code review, 11 October 2026, third round)
- Second dataset for the alert classifier: `tools/fetch_rcaeval_re23.py` downloads RCAEval RE2 and RE3 logs and metrics (720 files, about 570 MB, SHA-256 of every file pinned in `tools/rcaeval_re23_sha256.txt`; git-ignored), and `run_rcaeval_logs.py` runs the classifier / severity / no-log-alerts ablation and the product's promotion gate on each application (`results/rcaeval_logs.csv`, `.json`; report §B5). On RE2 Train Ticket the classifier adds 13.3 points (95% interval +4.4 to +24.4) and the gate promotes it there and on RE2 Online Boutique; elsewhere it adds at most one case and the gate keeps it out. Tests in `test_rcaeval_logs.py`.
- `perf/resources.py` measures one GAIA scoring job (35.7 s, 500 MB peak, about 215 KB of database a day); report §7 has an operating-cost estimate with its assumptions and cited prices.
- UML use-case diagram (`figures/use_cases.svg` and `.png`, drawn by `make_figures.py`), a data-retention table (§11) and numeric retrain and rollback triggers (§9).
- Report §B1: the shipped configuration (the gate keeps the classifier out on GAIA) is its own row, 71.0% at K = 20. `run_validate.py` now checks 189 numbers; 279 tests.

### Changed (code review, 11 October 2026, third round)
- The promotion gate's queue check compares the new model with what the batch falls back to if the model is refused (the active model, or severity rules), as well as with no log alerts (`_queue_check(..., fallback=...)`). On GAIA the fallback queue catches 871 of 1,223 held-out faults with 70 false items; the classifier still fails the gate.
- Two experiment-log notes said "Instructor review"; they now say "Code review".

### Changed (code review, 10 October 2026, second round)
- The alert classifier is no longer on by default. The promotion gate (`promotion_decision`) now also requires a new model to beat showing **no log alerts**, alert by alert and in the **whole held-out queue** (10 × missed faults + false items), and the first model is no longer promoted just because none is active. A model that fails stays a candidate and does not score the batch it was trained on (rules only or the active model does). On GAIA the classifier fails the queue test (848 vs 872 of 1,223 held-out faults caught), so GAIA runs on rules only. Tests in `test_model_registry.py`.
- `run_classifier_ablation.py` also writes `results/classifier_ablation_gaia.json`: why the severity row attaches no alerts, and the gate's decision on GAIA. `run_validate.py` checks 18 more numbers (93 in all) and that the gate still keeps the classifier out on GAIA.
- Report header fields on separate lines; report dated 11 October; design specification dated 10 October; coverage table in `docs/test_plan.md` refreshed; the `save_model` comment says what a crash between the two `os.replace` calls can still do.

### Fixed (code review, 10 October 2026, second round)
- Every `X-Forwarded-For` and `Forwarded` header line is read, not only the first, so a proxy that adds its own line cannot be bypassed by the client's line. Regression tests `test_every_forwarded_header_line_is_read` and `test_login_lock_holds_when_the_proxy_adds_its_own_xff_line`. `docs/deployment.md`: `TRUSTED_PROXIES` lists only the proxy's own addresses, never a client subnet.

### Added (code review, 10 October 2026, second round)
- `app/tests/conftest.py`: every test starts with the sign-in, proxy, ingest-limit and connector variables unset, so a shell with `APP_PASSWORD` exported no longer turns the suite red. 273 tests.

### Fixed (code review, 10 October 2026)
- CI audit job: `pip-audit` has no `--severity` option, so the job failed on every push. It now runs `uvx pip-audit -r requirements.lock --require-hashes --disable-pip`.
- Login lockout bypass behind a trusted proxy: the client address was the **leftmost** `X-Forwarded-For` value, which the client controls. It is now the rightmost hop that is not in `TRUSTED_PROXIES`. Regression test `test_login_lock_cannot_be_split_by_prepending_xff`.
- `X-Forwarded-Proto` marks the session cookie Secure only when it comes from a trusted proxy.
- `save_model` writes the model and its SHA-256 to temp files and swaps both in with `os.replace` under a lock, so a crash or two concurrent saves cannot leave a model whose digest does not match.
- Docker CMD states `--workers 1`; `docs/deployment.md` documents `TRUSTED_PROXIES` vs `uvicorn --forwarded-allow-ips`.

### Added (code review, 10 October 2026)
- `tools/fetch_gaia.py`: downloads the GAIA MicroSS run tables and the 37 memory series used, without an external unzip tool, and checks each file's SHA-256.
- `run_classifier_ablation.py` and `results/classifier_ablation_gaia.csv`: the full queue with log alerts chosen by the classifier, by severity, or not at all. At K = 20: 69.6%, 71.0%, 71.2%. The classifier adds no catches on GAIA; the report now says so (§5, §12, §B4). `run_validate.py` checks the six new numbers (75 in all).
- Report §C1a, use of AI tools.

### Changed (code review, 10 October 2026)
- One title everywhere: *AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices*, with *Alert Fatigue Reduction in Cloud Microservice Operations* as the subtitle.
- Report date, test counts (267), checked-number counts, project plan D6/T13, and the README pointer to the submitted proposal PDF.
- Submitted proposal (PDF and its Word source), `queue_flowchart.png` and `package.json` / `package-lock.json` committed; course material and draft Word files kept out of git.


### Added (submission hardening)
- `TRUSTED_PROXIES`: login lock and ingest rate limits key on the forwarded client only when the socket is a listed proxy.
- Ingest batch cap (`INGEST_MAX_BATCHES` / `INGEST_WINDOW_SECONDS` → 429) and alert-count cap (`INGEST_MAX_ALERTS` → 413).
- `POST /models/reset` quarantines a corrupt `registry.json`; `GET /models` returns 503 until then.
- Metric-median drift on `/monitoring` (same 0.5–2 band as alert volume).
- Backup/restore (`python -m product.backup`), JSON logs (`LOG_FORMAT=json`), audit-log rotation, paging-key cap (`PAGE_KEY_MAX`).
- CI `pip-audit`, Dependabot, tag-triggered image publish, `.gitattributes` (LF).

### Added (Phase 3, from the course brief review)
- Model registry: every trained model kept as a version with a card; promotion gate on held-out cost; rollback API (`/models`).
- Experiment log `app/results/experiments.jsonl` written by every experiment script.
- Production monitoring: `/metrics` (Prometheus) and `/monitoring` (checked health).
- Load test `app/perf/load_test.py` and results; AI-specific input tests; per-environment settings `app/deploy/*.env`.
- Documents: `docs/deployment.md`, `docs/mlops.md`, `docs/monitoring.md`, `docs/privacy_responsible_ai.md`.

### Fixed (Phase 3)
- A webhook row without a usable timestamp or source was buffered and made every later scoring job fail; rows are now validated.

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
