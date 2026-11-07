# Architecture

**System:** AI-Assisted On-Call Queue · **Version:** 1.2.0

Diagrams are Mermaid; GitHub renders them in place. Each one is drawn from the code, and file names are given so a reader can check.

---

## 1. Context

Who and what the system talks to.

```mermaid
flowchart LR
    oncall([On-call engineer]) -- "queue, Real/Noise marks" --> app
    lead([Team lead / operator]) -- "Connectors, Live switch, Evaluate my data" --> app
    am[Alertmanager] -- "POST /ingest/alertmanager\n(Bearer token)" --> app
    app[["On-call queue\n(FastAPI app)"]] -- "GET /api/v1/query_range" --> prom[(Prometheus)]
    app -- "Events API v2" --> pd[PagerDuty]
    app -- "incoming webhook" --> slack[Slack]
    app -- "Alert API" --> og[Opsgenie]
    researcher([Examiner / researcher]) -- "run_*.py, run_validate.py" --> lib[["Experiment scripts\n(src/ library)"]]
    lib -- "reads" --> gaia[(GAIA / NAB / SMD files)]
```

Only webhook-ingest jobs may page, and only in Live mode (ADR 0002).

---

## 2. Components

The code has two layers: a library that knows nothing about HTTP (`app/src/`) and a product layer that wraps it in an app, storage and connectors (`app/product/`).

```mermaid
flowchart TB
    subgraph web["app/product/web — HTTP layer"]
        app["app.py (wiring)\nmiddleware order + routers"]
        sec["security.py\nlogin, CSRF, Host,\nbody limit, audit"]
        routes["ops_routes · job_routes ·\nevaluate_routes"]
        ctx["context.py\nstore in use, paths"]
    end
    subgraph product["app/product — jobs and connectors"]
        cli["cli.py\nscore / evaluate / tune"]
        pipe["pipeline.py\nrun_job, score_alerts"]
        inputs["inputs.py\nload a batch per mode"]
        labels["labels.py\nReal / Noise marks"]
        paging["paging.py\nwho is paged, page marks"]
        store["store.py\nSQLite"]
        ops["ops.py\nops.json, SSRF check"]
        prom["prometheus_adapter.py\nfetch, no redirects"]
        notify["pagerduty.py, notifiers.py"]
        model["model_store.py · settings.py"]
    end
    subgraph lib["app/src — detection and evaluation library"]
        spikes["memory_spikes.py\njumps, leaks, gaps"]
        clf["classifier.py, features.py"]
        policy["policy.py, baseline.py\nfirst come"]
        queue["queue.py\ncausal daily queue"]
        scoring["scoring.py\none 'caught' rule,\none threshold search"]
        generic["generic.py\nEvaluate any data"]
        exp["experiment.py, eval_stats.py"]
    end
    app --> sec
    app --> routes
    routes --> ctx
    routes --> pipe
    routes --> ops
    routes --> generic
    ctx --> store
    cli --> pipe
    pipe --> inputs
    pipe --> labels
    pipe --> paging
    pipe --> store
    pipe --> model
    pipe --> spikes
    pipe --> clf
    pipe --> policy
    pipe --> queue
    inputs --> prom
    prom --> ops
    paging --> ops
    paging --> notify
    queue --> scoring
    spikes --> scoring
    generic --> queue
    exp --> queue
    exp --> scoring
```

| Module | Responsibility | Depends on |
|---|---|---|
| `product/app.py` | Wires the middleware (audit → body limit → login) and the routers; nothing else | web |
| `product/web/security.py` | Login, CSRF and Host checks, request-body limit, audit log | uploads |
| `product/web/*_routes.py` | One module per route group: ops (health, connectors, `/ops/*`), jobs (queue, marks, uploads, ingest, pages), evaluate | context, pipeline, ops, generic |
| `product/web/context.py` | The queue store in use (`use_store`) and file locations | store |
| `product/pipeline.py` | `score_alerts` and the job orchestrator: load → label → score → save model → page → store | inputs, labels, paging, src |
| `product/promotion.py`, `queue_check.py`, `training_report.py` | The promotion gate (pure rule over the held-out report), the three-way held-out queue score it reads, and the batch split and fit summaries; split out of `pipeline.py` | pipeline, model_store, src |
| `src/drift.py` | Input drift between the training window and the scored window (PSI, KS, classifier two-sample test); stored in each job and read by `product/monitoring.py` | entities, features |
| `product/inputs.py` | Read one batch for a mode (sample, GAIA, upload, ingest with Prometheus) | src, prometheus_adapter |
| `product/paging.py` | Decide whether a job may page, send to each channel, move the page marks | ops, pagerduty, notifiers |
| `product/store.py` | SQLite persistence; one transaction per call | — |
| `product/ops.py` | Operator state in `ops.json` (locked writes); SSRF check for Prometheus | — |
| `src/memory_spikes.py` | Jump, leak and gap detection; `Spike.open_at` | scoring |
| `src/policy.py`, `src/baseline.py` | Alert decisions under a daily budget, first come | config |
| `src/queue.py` | Build the causal queue; score it against faults | scoring |
| `src/scoring.py` | The one definition of a catch (same service, overlapping window) and the one lowest-cost threshold search | config |
| `src/generic.py` | Column detection, per-series tuning and the Evaluate report | memory_spikes, queue |

Design debt left after Phase 2: `src/generic.py` (about 700 lines) still mixes CSV sniffing, tuning and the report, and `policy.tune_threshold` (per-alert labels) is a separate search from `scoring.lowest_cost_threshold` (distinct faults), by design.

---

## 3. Sequence: a webhook batch that is scored and paged

`POST /ingest/alertmanager?score_now=1` in Live mode. File and function names are the ones in the code.

```mermaid
sequenceDiagram
    autonumber
    participant AM as Alertmanager
    participant API as web/job_routes.py
    participant ST as store.py (SQLite)
    participant PL as pipeline.run_job
    participant PG as paging.page_job
    participant OPS as ops.py
    participant PR as Prometheus
    participant LIB as src (detect, score, queue)
    participant CH as PagerDuty / Slack / Opsgenie

    AM->>API: POST /ingest/alertmanager (Bearer token)
    API->>API: BodyLimit (≤ MAX_INGEST_MB) · token check · audit
    API->>API: normalize_ingest (firing only)
    API->>ST: buffer_new_alerts (skip duplicates)
    API->>ST: peek_buffer
    API->>PL: run_job(mode="ingest")
    alt another job is running
        PL-->>API: JobBusy
        API-->>AM: 409 (alerts stay buffered)
    end
    PL->>OPS: get_prom_url
    PL->>OPS: assert_prom_url_safe (resolve host, check every address)
    PL->>PR: query_range (redirects refused)
    PR-->>PL: memory series
    PL->>ST: feedback marks → labels
    PL->>LIB: detect_jumps / detect_leaks / detect_gaps(now)
    PL->>LIB: classifier score → spike_review_decisions (first come)
    PL->>LIB: build_queue (≤ K a day, alerts ≤ share)
    PL->>PG: page_job(mode, shadow, alerts, decisions, shown)
    PG->>OPS: can_page? (Live + channel + password)
    loop each configured channel
        PG->>OPS: channel_cutoff(channel)
        PG->>CH: items shown since that channel's mark
        PG->>OPS: set_channel_watermark (if delivered)
    end
    PG->>OPS: set_page_watermark (if no channel failed)
    PL->>ST: save_job (decisions, jumps, meta)
    PL-->>API: job summary
    API->>ST: ack_buffer
    API-->>AM: 200 {received, duplicates, job}
```

In shadow mode steps 19–21 are replaced by a *would-have-paged* record (every job keeps one). Step 22 still runs, so turning Live on never pages the shadow period.

---

## 4. Deployment

```mermaid
flowchart LR
    subgraph host["Team server (Docker)"]
        subgraph container["container oncall-queue (non-root uid 10001)"]
            uv["uvicorn product.app:app\n:8080"]
        end
        vol[("volume queue-data\n/app/product/data\nqueue.db · ops.json · model.pkl\nsettings.json · evals/")]
        ro[("read-only mounts\ndata/gaia_raw, data/gaia_metrics")]
        uv --- vol
        uv --- ro
    end
    proxy["reverse proxy with TLS\n(optional; needs APP_PASSWORD)"] -->|":8080"| uv
    user([Browser]) -->|"https"| proxy
    user -. "127.0.0.1:8080\n(compose default)" .-> uv
    am[Alertmanager] -->|"POST /ingest/*"| uv
    uv -->|"http(s)"| prom[(Prometheus)]
    uv -->|"https"| saas["PagerDuty · Slack · Opsgenie"]
```

- The compose file publishes the port on `127.0.0.1` only. Sharing it needs `APP_PASSWORD`; a request that arrives through a proxy is refused without one.
- The image installs exact, hash-checked versions from `requirements.lock` (exported from `uv.lock`) and has a health check on `/health`.
- `Dockerfile` is the CPU image CI builds. `Dockerfile.gpu` plus the Compose profile `gpu` (`oncall-queue-gpu`) is optional and is not started by `docker compose up`. `/health` reports the active accelerator backend and whether shadow-note weights are enabled, present, or hash-checked. It still does not call Prometheus.
- One process, one worker. The job lock, the evaluation lock and the `ops.json` lock are in-process, so running several workers or replicas is not supported (ADR 0003).

---

## 5. Data model (SQLite, `product/store.py`)

```mermaid
erDiagram
    JOBS ||--o{ DECISIONS : "has"
    JOBS ||--o{ JUMPS : "has"
    DECISIONS ||--o| FEEDBACK : "marked (kind = alert)"
    JUMPS ||--o| FEEDBACK : "marked (kind = jump)"

    JOBS {
        text job_id PK
        text created_at
        text source "sample | gaia | upload | ingest"
        int budget_k
        real escalate_threshold
        real review_threshold
        int shadow_mode
        text meta_json "paging result, model, training report"
    }
    DECISIONS {
        int id PK
        text job_id FK
        text alert_id
        text timestamp
        text day
        text source
        text severity
        text message
        text action "escalate | review | suppress"
        real score
        text reason
        int during_memory_spike
    }
    JUMPS {
        int id PK
        text job_id FK
        text day
        text service
        text start
        text end
        real rise_gb
        int alerts_inside
        int escalated_inside
        text kind "jump | leak | gap"
        text confirm
        int shown
    }
    FEEDBACK {
        text kind PK "alert | jump"
        int item_id PK
        text verdict "real | noise"
        text service
        text start
        text end
        text created_at
    }
    INGEST_BUFFER {
        int id PK
        text received_at
        text payload_json
    }
    INGEST_SEEN {
        text fingerprint PK "sha256 of the alert"
        text seen_at
    }
```

`FEEDBACK` keeps the service and time window of the marked item, so marks survive when old jobs are pruned (NFR-9). `INGEST_BUFFER` and `INGEST_SEEN` stand alone: the buffer is acknowledged after a successful job, and fingerprints expire after `INGEST_DEDUP_HOURS`.

Operator state that is not relational lives in `ops.json` (Live override, keys, Prometheus URL, page marks); it is written under a lock through a unique temporary file.

---

## 6. Optional GPU training

The classifier can train its head with cuML, or with XGBoost on a CUDA device, and then stores a scikit-learn pipeline. `load_model` does not import those libraries. `src/accel.get_backend` resolves `auto` to sklearn even when cuML is installed; a GPU backend runs only when it is named, and falls back to sklearn when the device or the library is missing. Roles, the install, and what is deliberately not automatic are in `docs/GPU_LLM_ROLES.md`.

A local LLM can attach a short note to the shadow record (`would_have_paged.explanations`) when `LLM_EXPLAIN` is set. The note is written after the page decision, from fields known at that moment, and it is not sent to a channel. Missing weights or a failed completion leave the same items with an empty note list. The GGUF is pinned in `product/llm_pin.json` and checked by SHA-256 at load.

`LLM_TEMPLATE_FEATURES` can add a hash of the log template and a count of earlier copies to the classifier. It is off. The synthetic sample and the GAIA full queue both dropped (`docs/GPU_LLM_ROLES.md`: `hash-v1` 843 vs 851, MiniLM 856 vs 851, of 1223 faults). The default numeric columns stay the ones in `NUMERIC_COLUMNS`. `/monitoring` has a drift check for the mean of those prior counts; the check is `n/a` while the flag is off. Shadow-note latency, timeout rate, fallback rate, and GPU memory (when `nvidia-smi` answers) are on `/metrics`. A note is not a page.

Two further models can sit in front of the same queue. `METRIC_MODEL` (off) may add rule items from a causal forecast-error TCN. It stays off in 1.1.0: the published GAIA extract is memory only, so the TCN is dropped. `LEARNED_RANKER` (off) may skip low-scoring candidates when choosing the K slots. The GAIA ranker was a keep and the flag still stays off. Both load hash-checked files and refuse pickle. Neither pages. `RANKER_EXPLAIN`, `RANKER_ABSTAIN` and `RANKER_DISAGREE` (all off) may attach SHAP reasons, a needs-human-review list, and a disagreement flag to that shadow record. They do not remove a shown item and they are not passed to the pager. Flags, leakage controls and the measured decisions are in `docs/ML_MODELS.md`.
