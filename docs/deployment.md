# Deployment, environments and AI governance

**System:** AI-Assisted On-Call Queue · **Version:** 1.2.2

## 1. Where it runs

| Part | Where | Notes |
|---|---|---|
| Web app and API (FastAPI, uvicorn) | One Docker container on the team's own server or VM | Non-root user, health check on `/health`, image built from hash-pinned `requirements.lock`. Optional CUDA image: `Dockerfile.gpu` and `docker compose --profile gpu up --build oncall-queue-gpu`. The default image stays CPU |
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
| **Promotion** | — | A PR merges only when CI is green (lint, types, tests on two Python versions, coverage ≥ 85%, pip-audit, report check, Docker health) | A `v*` tag: CHANGELOG must list the version; the image is pushed to GHCR |

The same image runs in every environment. Only environment variables differ, and every variable is listed in `app/README.md`.

## 3. Scalability and reliability

- **Measured capacity** (`perf/load_test.py`, one container on a laptop): 50 concurrent users, 228 requests/s, p95 0.32 s, no errors. A scoring job over a 400-alert webhook batch takes 0.2 s. The full GAIA month (34,863 alerts, 1,438 jumps, with training) takes 54.9 s as one job (`perf/resources.py`, median of 3 runs on the same laptop, outside the container; `app/results/resources_gaia.json`). One on-call team sends far less than this.
- **Scaling path:** the job lock, evaluation lock and `ops.json` lock are in-process, so the app runs **one uvicorn worker in one container per team**; a second worker could run a job at the same time as the first. Login sessions, the login lock and the login/ingest counters are not the limit: they are in `auth.db` on the volume (§3a). To serve many teams, run one container per team (each with its own volume). For a shared deployment, the store moves to PostgreSQL, the locks to database advisory locks, and sessions from `auth.db` to that database (ADR 0003).
- **Reliability:** a busy job returns 409 and keeps the alerts buffered; failed scoring keeps the buffer; duplicates are not re-buffered; a failed page is retried on the next job only for the channel that failed; bad webhook rows are rejected instead of blocking the queue.
- **Backup:** `uv run python -m product.backup backup /path/to/copy` copies `queue.db`, `ops.json`, `secrets.json`, `settings.json`, `model.pkl`, `models/` and `audit.log`. `queue.db` is copied with SQLite's backup API, so a backup taken while the app runs is consistent and includes commits still in the write-ahead log. Restore with `uv run python -m product.backup restore /path/to/copy` onto a stopped container, then start it; restore deletes the lost database's `queue.db-wal` and `queue.db-shm` so SQLite cannot replay them onto the restored file. The backup folder holds `secrets.json`: keep it owner-only. The Docker volume `queue-data` is the live copy.

## 3a. Sessions and workers

Login sessions, the login lock and the ingest rate counters are kept in `auth.db` beside `ops.json` (`product/web/auth_store.py`; `AUTH_DB` moves it). A restart therefore keeps people signed in and keeps a lock an attacker earned. Session tokens are stored as SHA-256, so a copy of the file holds no usable cookie. The app still runs one worker (ADR 0003): the job, evaluation and `ops.json` locks are in-process, so do not raise `--workers` or run replicas in front of one SQLite file, or two jobs can race. Put TLS and `TRUSTED_PROXIES` on the reverse proxy in front of this single container. The image starts uvicorn with `--workers 1` for this reason.

## 3b. Client addresses behind a reverse proxy

The login lock, the ingest rate limit and the audit log key on the client address. Behind a proxy every request comes from the proxy, so the app needs the original client from a forwarded header, and must not let the client choose it.

- **`TRUSTED_PROXIES` (recommended).** List the proxy's own addresses (for example `TRUSTED_PROXIES=10.0.0.2/32`, or the few addresses of a proxy pool). Only when the socket is in that list does the app read `X-Forwarded-For` (or `Forwarded`). It reads **every** header line in order (some proxies add a new line instead of appending to the client's), walks the hops **from the right** and takes the first address that is not itself a trusted proxy. Proxies append the address they saw on the right, so anything a client writes further left, or in its own header line, is ignored and cannot split the login lock (`test_login_lock_cannot_be_split_by_prepending_xff`, `test_login_lock_holds_when_the_proxy_adds_its_own_xff_line`).
- **List only proxies, never a client subnet.** Every address in `TRUSTED_PROXIES` is skipped as "just a proxy" and is allowed to name the client. If the list covers the network the clients sit on (for example `10.0.0.0/8` on an office network), a client on it can connect directly and write any `X-Forwarded-For` it likes, and a forged hop that falls inside the list is skipped too. Keep the list to the proxy's own addresses, and keep the app's port reachable only from the proxy. `X-Forwarded-Proto: https` is also honoured only from a trusted proxy; otherwise set `COOKIE_SECURE=1`.
- **Alternative: uvicorn's own proxy handling.** Start uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy IP>` and leave `TRUSTED_PROXIES` empty. Uvicorn then rewrites the client address before the app sees it. Do not use `--forwarded-allow-ips='*'` on a port clients can reach directly: any client could then name its own address.
- **Without either**, forwarded headers are ignored and every client behind the proxy shares one login lock.

## 3c. Kubernetes with Helm

The chart is `app/deploy/helm/oncall-queue/`. It keeps the product's safety rules in the manifests: **shadow mode on**, **a password required** (the chart refuses to render without `auth.password` or `auth.existingSecret`), **one replica with the `Recreate` strategy** (SQLite and the job lock live in one process, ADR 0003), a non-root pod with a read-only root filesystem and all capabilities dropped, and probes on `/health`. The queue database, model registry and every model version live on one volume that Helm keeps if the release is removed or rolled back. Writable paths are volumes: `/app/product/data`, `/tmp` and `/app/data/sample_product` (the sample job writes there; a real run of the hardened image found this, so it is a mount and a chart test).

```bash
ONCALL_PASSWORD=... tools/deploy.sh <release> <namespace>       # install or upgrade, verify, roll back on failure
```

`tools/deploy.sh` runs `helm upgrade --install --atomic --wait` (Helm rolls the release back by itself if the pod never becomes ready), port-forwards, and runs `tools/smoke_test.py`: `/health`, shadow mode on, the expected version, the password enforced, `/monitoring` with the drift and challenger checks, `/metrics`, and on a first install a sample job. An upgrade skips the sample job so a live queue is not touched. If the smoke test fails the script runs `helm rollback` to the previous revision (on a first install it uninstalls the release). Set `KUBE_CONTEXT` on a shared machine so it cannot touch another cluster.

Optional values: `retrain.enabled=true` adds a CronJob that calls `POST /models/retrain` every 6 hours (the app decides whether a retrain is due); `serviceMonitor.enabled` and `prometheusRule.enabled` add the Prometheus Operator objects, with the alert rules of `docs/monitoring.md`.

**Release pipeline** (`.github/workflows/release.yml`, on a `v*` tag): the whole CI first, then build the image once, check the tag against `CHANGELOG.md`, the chart's `appVersion` and `product.__version__`, scan the image with Trivy (HIGH or CRITICAL with a fix fails the release), write an SBOM (SPDX), deploy that exact image to a throwaway kind cluster with the chart and smoke-test it, and only then publish to GHCR. CI also lints the chart and shellchecks the deploy script on every push.

## 4. AI governance

| Question | Answer in this system |
|---|---|
| **Who is responsible for the AI?** | The operator (on-call team lead). They decide when Live is on, which model version is active, and review the monitoring page. Changes are audited. |
| **What may the AI decide?** | Only the **order and selection** of items in the day's queue, and whether an alert is escalated, sent to a person (*ask*), or hidden with a reason. It never restarts services, closes incidents or changes infrastructure. |
| **What may it not decide?** | Anything outside the queue. Metric jumps and gaps come from rules, not the model. Paging needs Live mode, a channel and a password, all set by a person. |
| **When does a human review an output?** | Always at first: shadow mode shows what Live would have paged. Uncertain alerts inside a memory jump are sent to a person (*ask*). Every shown item can be marked Real or Noise. |
| **How is AI output identified?** | Each decision stores its action, score and reason (for example "policy top-k", "ask a person: inside memory jump", "severity threshold"). The job records which model version scored it and whether the batch used a trained model, the saved model, or rules only. |
| **How are wrong outputs handled?** | A Noise mark turns an alert into a negative label for the next training run; a Real mark into a positive one. If a model gets worse, the operator rolls back (`POST /models/{version}/activate`). |
| **How are model changes managed?** | Every trained model is a new version with a card. It becomes active only if, on held-out data, it beats showing no log alerts (alert by alert and in the whole queue) and is no worse than the active model on the same alerts (promotion gate). Until then rules only, or the active model, scores the batches. See `docs/mlops.md`. |
| **What data goes to an external AI service?** | None. The model runs in the container. Paging services receive an item summary (service, time, kind, alert message cut to 300 characters) only in Live mode. |
