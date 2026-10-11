# Monitoring and evaluation

**System:** AI-Assisted On-Call Queue · **Version:** 1.0.4

The running app exposes two endpoints (`product/monitoring.py`):

- **`GET /metrics`**: Prometheus text format, for the team's Prometheus to scrape and alert on.
- **`GET /monitoring`**: the same signals as JSON, each with an `ok` / `warn` / `n/a` verdict against the targets below, and an overall `status`.

Counters are per process and reset on restart; model and data signals are read from the database at scrape time.

## 1. What is monitored

| Area | Signal | Where | Target | Why this target |
|---|---|---|---|---|
| **AI performance** | Precision from marks: share of marked items the on-call engineer called Real (alerts and jumps, and each separately) | `/monitoring` → `model`; gauge `oncall_precision_from_marks` | ≥ 0.70 once there are ≥ 20 marks | Offline precision is 90–94% at K = 20 (FINAL_REPORT §5); 0.70 leaves room for real-world noise and still means most interruptions are worth it. Fewer than 20 marks is too few to judge. |
| **AI performance** | Model used per job (trained / saved / rules only), active model version, promotion decisions | Job meta, `GET /models` | A candidate that fails the gate is not activated | See `docs/mlops.md` |
| **System performance** | Request latency by route (histogram), p95 of interactive routes | `oncall_http_request_duration_seconds`; `/monitoring` → `latency` | p95 ≤ 1 s | Measured p95 0.32 s at 50 concurrent users; 1 s is where a page starts to feel slow |
| **System performance** | Requests by route and status class; 5xx rate | `oncall_http_requests_total`; `/monitoring` → `errors` | 5xx ≤ 2% | 0 errors in 3,000 load-test requests; 2% catches a real fault without flapping |
| **System performance** | Scoring jobs by mode and outcome (ok / failed / busy), job duration | `oncall_jobs_total`, `oncall_job_duration_seconds` | No failed jobs | A failed job means alerts wait in the buffer |
| **System performance** | Pages by channel and outcome | `oncall_pages_total`; `/monitoring` → `paging` | 0 failed | A failed page is an unseen incident |
| **Availability** | `/health` (database reachable, no external calls) | Docker health check, load balancer | > 99% during a pilot | Checked every 30 s by Docker |
| **Data** | Alert volume of the last ingest batch ÷ median of the earlier ones | `oncall_alert_volume_ratio`; `/monitoring` → `alert_volume_drift` | between 0.5 and 2 | A batch twice or half the usual size means an alert storm or a broken sender, and the model's inputs have shifted |
| **Data** | Per series: median of its last hour in the last job ÷ median of the same series in earlier jobs (worst series shown; at least 3 earlier jobs) | `oncall_metric_median_ratio`; `/monitoring` → `metric_value_drift`, `data.metric_drift_series` | between 0.5 and 2 for every series | A series that jumps to a new scale (units change, scrape broken, one service leaking) is input drift the alert-count check would miss. Per series and on the last hour, because ingest jobs re-score the whole history and one fleet-wide median hides a single service |
| **Data** | Monitoring gaps shown in the last job | `oncall_last_job_gap_items`; `/monitoring` → `monitoring_gaps` | ≤ 3 | 11% of GAIA faults fell in monitoring gaps; several gaps at once means the metric pipeline is broken |
| **Data** | Rejected ingest rows, duplicates | `/ingest` response (`rejected`, `duplicates`); `oncall_ingest_alerts_total{result}` | Rejected rows investigated | Bad rows are refused instead of blocking the queue |
| **Data** | Ingest buffer waiting to be scored | `oncall_ingest_buffer_batches` | Near 0 between jobs | A growing buffer means jobs are not running |
| **AI / model** | Output distribution: escalated / asked / hidden per job; items shown by kind | Job meta; Queue page | Watch for sudden shifts | A model that suddenly escalates much more or less has drifted |

## 2. Alerting on the monitor

Example rules for the team's Prometheus:

```yaml
groups:
  - name: oncall-queue
    rules:
      - alert: OncallQueueUnhealthy
        expr: oncall_health_ok == 0
        for: 15m
      - alert: OncallQueueErrors
        expr: sum(rate(oncall_http_requests_total{status="5xx"}[10m])) / sum(rate(oncall_http_requests_total[10m])) > 0.02
        for: 10m
      - alert: OncallQueuePageFailures
        expr: increase(oncall_pages_total{outcome="failed"}[30m]) > 0
      - alert: OncallQueueLowPrecision
        expr: oncall_precision_from_marks < 0.7 and oncall_feedback_marks >= 20
        for: 1d
```

## 3. Evaluation criteria

| Metric | Target | Measured | Evidence |
|---|---|---|---|
| p95 response time, 50 concurrent users | ≤ 1 s | 0.32 s | `app/results/perf/load_test_50_users.json` |
| Throughput | ≥ 20 req/s | 228 req/s | same |
| Successful requests under load | ≥ 99% | 100% (3,000 requests) | load tests at 20 and 50 users |
| Scoring job, 400-alert webhook batch | ≤ 5 s | 0.2 s | `oncall_job_duration_seconds` |
| Full GAIA month (34,863 alerts) | ≤ 5 min | 54.9 s | `perf/resources.py`, median of 3 runs (`app/results/resources_gaia.json`) |
| Automated tests passing | 100% | 301 / 301 | CI |
| Line coverage | ≥ 85% | 90% | CI |
| Faults caught at K = 20 (detector vs Prometheus rate rule) | beats it | 72.6% vs 68.9%, 8/8 weeks | `run_validate.py` |
| False items a day at K = 20 | fewer than the rate rule | 1.0 (detector), 1.8 (full queue) vs 2.1 | same |
| Time from fault start to confirmation | ≤ 1 min | 0.72 min median | same |
| Precision from on-call marks (in production) | ≥ 0.70 | to be measured in a pilot | `/monitoring` |
| Critical security issues | 0 | 0 critical. R1 through R8 are closed and tested | `docs/threat_model.md` |

Targets are set for a student prototype that could run a pilot: tight enough to catch a real problem, loose enough not to page the team about the monitor itself.
