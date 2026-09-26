# Connectors — Phase 2

Shadow mode is **on by default** (`SHADOW_MODE=1`). Decisions are stored and shown in the UI. Nothing is sent to PagerDuty until you turn shadow mode off **and** set `PAGERDUTY_ROUTING_KEY`.

Open http://127.0.0.1:8080 — the **Connectors** cards show whether Prometheus, Alertmanager, and PagerDuty are wired. The routing key is never shown. `GET /connectors` is the same status as JSON. **Score buffer** runs a job from webhook batches already received.

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

Escalations are sent only when **both** are true:

1. `SHADOW_MODE=0` (or the job is run with `"shadow_mode": false`)
2. `PAGERDUTY_ROUTING_KEY` is set (Events API v2 integration key)

Each escalated alert becomes one `trigger` event (dedup key = alert id). Each **shown metric jump or monitoring gap** also becomes one event (dedup key = kind + service + start, so re-scoring the same history does not open a second incident). Failures are stored on the job (`meta.pagerduty`) and do not stop scoring. "Ask" and "hide" are never paged.

```text
SHADOW_MODE=0
PAGERDUTY_ROUTING_KEY=your-events-api-integration-key
```

Leave shadow mode on until you have checked a week of queues against Real / Noise marks.

## Prometheus memory

Set:

```text
PROM_URL=http://localhost:9090
# optional; default is container_memory_working_set_bytes{container!="", container!="POD"}
PROM_QUERY=container_memory_working_set_bytes{namespace="prod", container!=""}
# optional; label whose value matches the alert "source" field
PROM_SERVICE_LABEL=app
```

When scoring `ingest` mode with `PROM_URL` set, the app pulls 14 days of memory and feeds the same memory-jump detector used for GAIA CSVs. A jump is a rise of more than 0.5 GB over the median of the previous 15 minutes, for at least 2 points.

- Use one memory metric only, in bytes. Mixing `usage_bytes` and `working_set_bytes` double-counts.
- The step is widened automatically so each series stays under Prometheus's 11,000-point limit (14 days gives a 121 s step).
- Without `PROM_SERVICE_LABEL`, the service is taken from `service`, `app`, or `container`; pod names have their replica hash removed (`webservice1-7d9f8c6b5-x2k4p` becomes `webservice1`). Replicas of one service are summed.

## Retrain note for UAE / cloud teams

GAIA-fitted weights are for the public demo. For Careem, e&, Mashreq, or any other platform:

1. Keep this app and the escalate / ask / hide policy.
2. Export ~2 weeks of their alerts + container memory.
3. Label faults from incident tickets (or SRE review).
4. Retrain with the same week-1 / week-2 protocol (`run_memory_clf.py` pattern).

Do not deploy GAIA-only weights as production scoring for a live queue.
