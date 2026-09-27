# On-call alert queue — product scope

## End user

**Primary:** cloud microservice on-call engineer — the person who can only check a few alerts tonight.

**Secondary:** SRE / platform on-call when the jump is on shared infra (Redis, DB, cluster).

Not the end customer of the app (rider, bank client). They benefit when the on-call person sees the real fault sooner.

## What this product does

One daily list of at most **K** items, first come first served (nothing is chosen with hindsight):

1. **Metric jumps**, shown when confirmed (rise more than 0.5 GB — or 30% in relative mode — above the last 15 minutes, at least two points).
2. **Slow leaks** (+0.5 GB over the last 3 hours) and **monitoring gaps** (a service silent for 10 minutes).
3. **Log alerts** — **escalate** (send now), then **ask** — using at most 25% of the slots. An alert inside a shown jump rides on it.
4. The rest are **hidden**.

A jump with no alerts inside is still shown: it may be a fault the logs missed. With no labels the app runs on rules only; Real / Noise marks train a model later.

## Phase 1 — local web app

- Browser UI at `/` (port 8080)
- Run **sample** (one synthetic day: jump, monitoring gap, slow leak, and a few log lines) or **GAIA**, or upload alerts CSV (+ optional memory CSV and faults CSV)
- Change K (5 / 10 / 20 / 50)
- FastAPI + SQLite + Docker Compose

## Phase 2 — connectors and labels

- `POST /ingest/alerts` webhook buffer
- Prometheus memory adapter (`PROM_URL`); one metric, step capped under 10,000 points
- **Shadow mode** (default): decisions are stored; nothing is paged. Each job records `would_have_paged` (what Live would have opened) for demos.
- **Real / Noise** on every alert and jump. Later runs use those marks as labels. Real window = jump span, or ±5 minutes around a Real alert. Noise overrides a Real window.
- Saved model (`product/data/model.pkl`). Small webhook batches are scored with it. Retrain reminder after 7 days or new Real marks (`GET /model`).
- Optional login: set `APP_PASSWORD` (user defaults to `oncall`). `/health` stays open.

## Out of scope

- Slack / Opsgenie paging (PagerDuty is built)
- Multi-tenant SaaS, SSO, bank compliance packs
- Full GAIA business / trace archives

## GAIA vs company use

GAIA MicroSS is the **public proof** (train week 1, test the next 55 days).

A company would:

1. Keep this app and the escalate / ask / hide policy
2. Feed **their** alerts and metrics (memory in bytes, or any metric with `JUMP_MODE=relative`); try an export first with `python -m product.cli score`
3. Mark items Real / Noise in shadow mode
4. Retrain on those labels — do **not** ship GAIA-fitted weights unchanged

## Sector

Cloud microservice operations (online platforms). One on-call queue, many small services.
