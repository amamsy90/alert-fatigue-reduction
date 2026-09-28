# Deployment, environments and AI governance

**System:** Budgeted Incident Detection · **Version:** 1.0.0

## 1. Where it runs

| Part | Where | Notes |
|---|---|---|
| Web app and API (FastAPI, uvicorn) | One Docker container on the team's own server or VM | Non-root user, health check on `/health`, image built from hash-pinned `requirements.lock` |
| Database | SQLite file on a Docker volume (`queue-data`) next to the app | One process; ADR 0003 explains why and what would change to scale out |
| AI model | In the same container: scikit-learn logistic regression, loaded from the model registry on the same volume | No external AI API. Nothing leaves the server to be scored |
| Metrics source | The team's Prometheus (pulled) and Alertmanager (pushes to `/ingest/alertmanager`) | The Prometheus URL is SSRF-checked |
| Paging | PagerDuty, Slack, Opsgenie over HTTPS | Only in Live mode, only from webhook-ingest jobs |
| Access | Browser → reverse proxy with TLS → container | `APP_PASSWORD` required behind a proxy; the compose file binds `127.0.0.1` by default |

Infrastructure is local (on-premises or the team's cloud VM). No managed cloud AI service is needed, so there is no per-request AI cost and no data leaves the team's network except the paging summaries.

## 2. Environments

| | Development | Test | Production |
|---|---|---|---|
| **Purpose** | Build and try features | Prove a change before it reaches users | Serve the on-call team |
| **Where** | Developer laptop | GitHub Actions on every push and PR; a shadow instance next to production | Team server (Docker Compose) |
| **How to start** | `uv sync --group dev` then `uv run uvicorn product.app:app --reload` (or `docker compose up`) | CI workflow; shadow instance: `ENV_FILE=deploy/test.env docker compose up -d` | `ENV_FILE=deploy/prod.env docker compose up -d` |
| **Data** | Built-in sample day, GAIA, NAB, uploads | Synthetic data in tests; the shadow instance gets a copy of real Alertmanager traffic | Real alerts and metrics |
| **Paging** | Never (no channel keys) | Never (shadow); every job records *would have paged* | After a shadow period and a deliberate Live switch |
| **Login** | None (loopback only) | Test password | `APP_PASSWORD`, `INGEST_TOKEN`, TLS at the proxy |
| **Settings** | `deploy/dev.env` | `deploy/test.env` | `deploy/prod.env.example` → `deploy/prod.env` (not in git) |
| **Promotion** | — | A PR merges only when CI is green (lint, types, 226 tests on two Python versions, coverage ≥ 85%, report check, Docker health) | A release tag; the image is rebuilt from the lock file |

The same image runs in every environment. Only environment variables differ, and every variable is listed in `app/README.md`.

## 3. Scalability and reliability

- **Measured capacity** (`perf/load_test.py`, one container on a laptop): 50 concurrent users, 228 requests/s, p95 0.32 s, no errors. A scoring job over a 400-alert webhook batch takes 0.2 s; the full GAIA month (34,863 alerts, 1,438 jumps, with training) takes 27 s. One on-call team sends far less than this.
- **Scaling path:** the job lock, evaluation lock and `ops.json` lock are in-process, so the app runs one worker. To serve many teams, run one container per team (each with its own volume). For a shared deployment, the store moves to PostgreSQL and the locks to database advisory locks (ADR 0003).
- **Reliability:** a busy job returns 409 and keeps the alerts buffered; failed scoring keeps the buffer; duplicates are not re-buffered; a failed page is retried on the next job only for the channel that failed; bad webhook rows are rejected instead of blocking the queue.

## 4. AI governance

| Question | Answer in this system |
|---|---|
| **Who is responsible for the AI?** | The operator (on-call team lead). They decide when Live is on, which model version is active, and review the monitoring page. Changes are audited. |
| **What may the AI decide?** | Only the **order and selection** of items in the day's queue, and whether an alert is escalated, sent to a person (*ask*), or hidden with a reason. It never restarts services, closes incidents or changes infrastructure. |
| **What may it not decide?** | Anything outside the queue. Metric jumps and gaps come from rules, not the model. Paging needs Live mode, a channel and a password, all set by a person. |
| **When does a human review an output?** | Always at first: shadow mode shows what Live would have paged. Uncertain alerts inside a memory jump are sent to a person (*ask*). Every shown item can be marked Real or Noise. |
| **How is AI output identified?** | Each decision stores its action, score and reason (for example "policy top-k", "ask a person: inside memory jump", "severity threshold"). The job records which model version scored it and whether the batch used a trained model, the saved model, or rules only. |
| **How are wrong outputs handled?** | A Noise mark turns an alert into a negative label for the next training run; a Real mark into a positive one. If a model gets worse, the operator rolls back (`POST /models/{version}/activate`). |
| **How are model changes managed?** | Every trained model is a new version with a card. It becomes active only if its held-out cost is no higher than the active model's on the same alerts (promotion gate). See `docs/mlops.md`. |
| **What data goes to an external AI service?** | None. The model runs in the container. Paging services receive an item summary (service, time, kind, alert message cut to 300 characters) only in Live mode. |
