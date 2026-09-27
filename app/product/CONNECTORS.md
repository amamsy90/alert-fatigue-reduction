# Connectors — Phase 2

Shadow mode is **on by default** (`SHADOW_MODE=1`, overridable from the UI into `ops.json`). Decisions are stored and shown in the UI. Nothing is sent to PagerDuty until you turn **Live** on **and** configure a routing key — and only **ingest** jobs page (sample / GAIA / Evaluate never do).

Open http://127.0.0.1:8080 — the **Connectors** cards show Shadow vs Live, whether a routing key is configured (last4 only; full key never shown), and last-job PagerDuty meta. `GET /connectors` is the same status as JSON. Use the ops controls to switch mode, save/clear a key, send a test page, or **Test Prometheus**. **Score buffer** runs a job from webhook batches already received.

## Webhook alerts

Push alerts into the buffer:

```bash
curl -s http://127.0.0.1:8080/ingest/alerts ^
  -H "Content-Type: application/json" ^
  -d "{\"alerts\":[{\"timestamp\":\"2026-01-12T10:00:00Z\",\"source\":\"webservice1\",\"severity\":\"warning\",\"message\":\"login failed\"}],\"score_now\":true,\"budget_k\":20}"
```

- `score_now: false` — only buffer  
- `score_now: true` — drain buffer and run a job  

Or buffer first, then `POST /jobs/run` with `{"mode":"ingest","budget_k":20}`.

When `APP_PASSWORD` is set, senders must log in too: add `-u oncall:<password>` to curl, or in Alertmanager set `basic_auth` on the webhook receiver:

```yaml
receivers:
  - name: oncall-queue
    webhook_configs:
      - url: http://oncall-queue:8080/ingest/alerts
        http_config:
          basic_auth:
            username: oncall
            password_file: /etc/alertmanager/oncall_password
```

Or, without a login for the whole app, set `INGEST_TOKEN` and send it as a bearer token (only `/ingest/*` accepts it; a wrong token gets 401):

```yaml
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/oncall_ingest_token
```

If scoring a `score_now` batch fails, the alerts stay in the buffer for the next run.

Without `APP_PASSWORD` the app answers only this machine and private networks (loopback, 10/8, 172.16/12, 192.168/16 — Docker's bridge included). Requests from public addresses get 403 until you set a password (or `ALLOW_OPEN_ACCESS=1`).

Point Alertmanager at the same URL. The app accepts Alertmanager's own webhook (version 4) as well as the `{alerts: [...]}` list. Firing alerts become queue rows; resolved alerts are ignored. Service comes from labels `service`, then `app`, `container`, `job`, `instance`.

```yaml
receivers:
  - name: oncall-queue
    webhook_configs:
      - url: http://oncall-queue:8080/ingest/alertmanager
        send_resolved: false
```

To score as soon as a webhook arrives, add `?score_now=1` (needs a saved model, or at least 10 labelled alerts). Otherwise buffer, then `POST /jobs/run` with `{"mode":"ingest","budget_k":20}`.

## PagerDuty

Shadow mode is **on by default**. Demos must never page.

### Safe UI path (recommended)

On the main queue **Connectors** panel:

1. **Save routing key** (Events API v2 integration key). The API and UI never show the full key — only `configured` and `last4`.
2. **Switch to Live** — type `LIVE` to confirm. Live ON is refused if no key is configured (saved or `PAGERDUTY_ROUTING_KEY` env).
3. Optional: **Send test page** (only when Live + key).
4. Switch back to **Shadow** any time.

Overrides persist in `product/data/ops.json` (or `OPS_PATH`). Env `SHADOW_MODE` / `PAGERDUTY_ROUTING_KEY` are the bootstrap defaults when no override is saved.

### When a page is actually sent

Escalations are sent only when **all** are true:

1. Effective mode is **Live** (ops override or `SHADOW_MODE=0`)
2. A routing key is configured (ops file or `PAGERDUTY_ROUTING_KEY`)
3. The job mode is **`ingest`** (live webhook / buffer score)

**Hard safety:** `mode=sample`, GAIA, Evaluate-my-data / upload paths **never** call `notify_escalations`, even if Live is on. Only ingest jobs page.

Each escalated alert becomes one `trigger` event (dedup key = alert id). Each **shown metric jump, slow leak or monitoring gap** also becomes one event (dedup key = kind + service + start). Only metric items confirmed **since the last ingest job** are paged (the first job looks back `PAGE_LOOKBACK_MINUTES`, default 60), so turning Live on never pages the 14 days of history the job re-scores. The mark advances in Shadow too, and is kept if a send fails so the next job retries. Failures are stored on the job (`meta.pagerduty`) and do not stop scoring. "Ask" and "hide" are never paged.

Every scored job also stores `meta.would_have_paged` — the same set Live *would* have opened (`count`, `spikes`, `alerts`, short `items` list). In Shadow the UI shows: “In Live mode this run would have opened N PagerDuty incidents.” Sample never pages, but would-have still counts (useful for demos).

### API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/connectors` | Status: Shadow/Live, key configured + last4, last job `pagerduty` meta |
| `POST` | `/ops/shadow` | `{"shadow_mode": false, "confirm": "LIVE"}` or `{"shadow_mode": true}` |
| `POST` | `/ops/pagerduty/key` | `{"routing_key": "…"}` — save key |
| `DELETE` | `/ops/pagerduty/key` | Clear saved key (env key still applies if set) |
| `POST` | `/ops/pagerduty/test` | One test trigger; Live + key required |

```text
# Optional env bootstrap (UI override wins once saved)
SHADOW_MODE=1
PAGERDUTY_ROUTING_KEY=your-events-api-integration-key
```

Leave Shadow on until you have checked a week of queues against Real / Noise marks.

## Slack and Opsgenie

Same gate as PagerDuty: Live mode, ingest jobs only, only items new since the last job, and every configured channel gets the same list (shown jumps / leaks / gaps, then escalated alerts). Live can be turned on with any one channel.

| Channel | Configure | What is sent |
|---|---|---|
| Slack | Save an incoming-webhook URL on the Connectors panel (`POST /ops/slack/webhook`, `{"webhook_url": ...}`) or set `SLACK_WEBHOOK_URL`. Must be `https://`. | **One message per job** listing up to 20 items ("… and N more"). Nothing is posted when there is nothing new. |
| Opsgenie | Save an API-integration key (`POST /ops/opsgenie/key`, `{"api_key": ...}`) or set `OPSGENIE_API_KEY`. EU region: `OPSGENIE_API_URL=https://api.eu.opsgenie.com`. | **One alert per item**, `alias` = the same dedup key as PagerDuty (kind + service + start, or the alert id), priority P1 critical / P2 error / P3 warning / P5 info, tags `oncall-queue`, `<kind>`. |

`DELETE` the same paths to clear a saved secret; `POST /ops/slack/test` and `/ops/opsgenie/test` send one test item (Live only). Secrets are never returned in full (last 4 characters only) and never appear in error messages. Per-channel results are on the job under `meta.pagerduty.channels`.

## Prometheus memory

### Configure

Set `PROM_URL` in the environment, **or** save it from the Connectors panel (stored in `ops.json` like the PagerDuty key — UI override wins once saved):

```text
PROM_URL=http://localhost:9090
# optional; default is container_memory_working_set_bytes{container!="", container!="POD"}
PROM_QUERY=container_memory_working_set_bytes{namespace="prod", container!=""}
# optional; label whose value matches the alert "source" field
PROM_SERVICE_LABEL=app
```

`PROM_QUERY` and `PROM_SERVICE_LABEL` are **env-only** (not saved in ops.json).

### Test pull (Connectors UI)

On the main queue **Connectors** panel: **Test Prometheus** (enabled when a URL is configured). That calls `GET`/`POST /ops/prometheus/test`, which probes `/api/v1/status/buildinfo` and runs a short ~2h range query. Response is compact only:

```json
{"ok": true, "series_count": 3, "points": 12, "url_host": "localhost", "version": "2.54.0"}
```

Never dumps series payloads. On failure: `{ok: false, series_count: 0, points: 0, error, url_host}`.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/ops/prometheus/url` | `{"url": "http://…"}` — save URL |
| `DELETE` | `/ops/prometheus/url` | Clear saved URL (env still applies) |
| `GET`/`POST` | `/ops/prometheus/test` | Probe + short query; counts only |

When scoring `ingest` mode with a Prometheus URL configured (ops or env), the app pulls 14 days of memory and feeds the same memory-jump detector used for GAIA CSVs. A jump is a rise of more than 0.5 GB over the median of the previous 15 minutes, for at least 2 points.

- Use one memory metric only, in bytes. Mixing `usage_bytes` and `working_set_bytes` double-counts.
- The step is widened automatically so each series stays under Prometheus's 11,000-point limit (14 days gives a 121 s step).
- Without `PROM_SERVICE_LABEL`, the service is taken from `service`, `app`, or `container`; pod names have their replica hash removed (`webservice1-7d9f8c6b5-x2k4p` becomes `webservice1`). Replicas of one service are summed.

## Retrain note for UAE / cloud teams

GAIA-fitted weights are for the public demo. For Careem, e&, Mashreq, or any other platform:

1. Keep this app and the escalate / ask / hide policy.
2. Export ~2 weeks of their alerts + container memory.
3. Label faults from incident tickets (or SRE review).
4. Retrain with the same early-window / later-window protocol (`src/experiment.py` pattern).

Do not deploy GAIA-only weights as production scoring for a live queue.
