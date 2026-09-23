# Production conversion plan (implementable tool)

See [product/README.md](product/README.md) for end user, phases, and out of scope.

**Built:** Phase 1 local web app; Phase 2 webhook + Prometheus; shadow mode; Real / Noise marks; saved model and retrain reminder; optional login.

**Core library:** `src/` classifier, memory jumps (0.5 GB over 15-minute median), shared-budget policy (jumps first).

**Product layer:** `product/` FastAPI, SQLite, UI, connectors.

**Still later:** Slack / Opsgenie when `SHADOW_MODE=0`. Alertmanager webhooks and PagerDuty escalations are built.
