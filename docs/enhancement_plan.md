# Enhancement plan (closed for submission)

Items from the course-project review. Each one has a regression test whose expected result comes from the requirement.

| # | Item | Where |
|---|---|---|
| 1 | Login lock uses the forwarded client (rightmost hop that is not a trusted proxy) only when `TRUSTED_PROXIES` lists the socket | `product/web/security.py`, `test_login_lock_uses_forwarded_client_only_behind_a_trusted_proxy` |
| 2 | In-memory sessions: one uvicorn worker, one container per team | `docs/deployment.md`, ADR 0003 |
| 3 | Ingest alert-count 413 and batch-rate 429 | `INGEST_MAX_ALERTS`, `INGEST_MAX_BATCHES` |
| 4 | Corrupt registry 503; `POST /models/reset` with `RESET` | `product/model_store.py` |
| 5 | HTTPS CONNECT proxy; Slack fail retries Slack only | `test_https_prometheus_fetch_tunnels_through_https_proxy`, `test_failed_slack_is_resent_and_pagerduty_is_not` |
| 6 | Browser path: 401 → login cookie → sample job → `/queue` | `tests/test_smoke.py` |
| 7 | `pip-audit` (hash-checked lock, any known vulnerability fails) in CI; Dependabot | `.github/workflows/ci.yml`, `.github/dependabot.yml` |
| 8 | Tag `v*` builds the image; CHANGELOG must list the version; GHCR | `.github/workflows/release.yml` |
| 9 | Backup/restore of queue.db, ops, models, audit log | `product/backup.py` |
| 10 | Metric-median drift on `/monitoring` (band 0.5–2) | `product/monitoring.py` |
| 11 | Cap paging keys; rotate `audit.log` | `PAGE_KEY_MAX`, `AUDIT_LOG_MAX_BYTES` |
| 12 | `LOG_FORMAT=json` | `product/logging_config.py` |

Housekeeping: `.gitattributes` (`eol=lf`).
