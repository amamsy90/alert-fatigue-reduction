# Production plan (implementable tool)

See [product/README.md](product/README.md) for the end user and scope, [../USE_YOUR_DATA.md](../USE_YOUR_DATA.md) for the step-by-step guide.

**Built**

- **Queue:** one causal daily budget K, first come first served — metric jumps shown when confirmed, slow leaks, monitoring-gap items (≤ 25% of K), log alerts (≤ 25% of K; alerts inside a shown jump ride on it). Optional grouping across services and carry-over of unused slots.
- **Detectors:** memory jumps (0.5 GB over the 15-minute median), relative mode, a unit-free robust mode for any metric, slow-leak rule, gap detection.
- **Any data:** *Evaluate my data* page and `cli evaluate` / `tune` — column detection, tuning on the early part, report against a random-chance line, one click to use the tuned settings live.
- **Operations:** web app (FastAPI + SQLite), shadow mode, Real / Noise marks → retraining, saved model with SHA-256 check, Alertmanager webhook with `INGEST_TOKEN`, Prometheus adapter, PagerDuty for alerts, jumps and gaps, login (`APP_PASSWORD`), private-network-only without a password, Docker.

**Core library:** `src/` (queue, detectors, generic evaluation, classifier, statistics). **Product layer:** `product/` (app, CLI, store, connectors, datasets).

**Still later:** Slack / Teams / Opsgenie notifications; importing incidents from PagerDuty or Jira; a per-series jump size learned from Real marks.
