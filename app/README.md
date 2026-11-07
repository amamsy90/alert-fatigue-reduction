# On-call queue — budgeted incident detection from metrics

Your services raise more alerts than one person can read. This tool gives on-call **at most K items a day**, in the order they happen:

1. **Metric jumps** — a service's metric suddenly rising well above its own last 15 minutes (e.g. +0.5 GB of memory, or +30% of anything in relative mode). Shown ~30 s after the jump starts.
2. **Slow leaks** — memory climbing +0.5 GB over its own last 3 hours, which a 15-minute rule (or a Prometheus `delta()` alert) never sees.
3. **Monitoring gaps** — a service stopped sending metrics for 10 minutes. You are blind there; that is worth knowing.
4. **Log alerts** — ranked by severity (no labels needed) or by a model trained on your Real / Noise marks. They may use at most a quarter of the slots, and an alert inside a shown jump rides on it instead of taking a slot.

It runs in **shadow mode** by default: nothing is paged until you turn **Live** on from Connectors (with a routing key). Demos (sample / GAIA / Evaluate) never page. After any run the UI shows a **would-have-paged** card (what Live would have opened) so a sales demo can show PagerDuty value without sending.
Buyer path on the main page: Run sample → Evaluate shop/my data → Review queue + would-have → Connectors (key, stay Shadow until pilot agrees).

On the public GAIA benchmark it catches **72.6%** of injected faults at 20 items a day (the best possible is 84.9%; 98% of the rest are metric outages or a full budget), against **68.9%** for a Prometheus `delta()` rule and **0%** for log severity, with fewer false items. On synthetic faults added to real series it catches 0.5–2 GB steps and spikes 98–100% of the time and slow leaks 73–99%. Details: [../FINAL_REPORT.md](../FINAL_REPORT.md).

**Not microservices?** It works on any number over time — orders, sign-ups, sensor readings, ad clicks, notification or ticket counts. Open **/evaluate** in the web app, upload your metrics (plus past problems and alerts if you have them), and the app tunes itself on the early part of your data and tells you — against a random-chance line — whether it would have caught your past problems. Step-by-step guide: [../USE_YOUR_DATA.md](../USE_YOUR_DATA.md).

---

## Try it in two minutes (no server, no labels)

```bash
cd app
pip install -r requirements.txt
export PYTHONPATH=$(pwd)            # PowerShell: $env:PYTHONPATH = (Get-Location).Path
python examples/make_example.py    # tiny synthetic export: one jump, one drift, one outage
python -m product.cli score --alerts examples/alerts.csv --metrics examples/metrics.csv --out queue.csv
```

On your own data:

| File | Columns |
|---|---|
| alerts csv | `timestamp, source, severity, message` (optional `alert_id, alert_type`) |
| metrics csv | `timestamp, value, service` — one row per sample; `service` must match alert `source` |
| faults csv (optional) | `fault_id, start, end, component` — past incidents; enables training and a score |

Metric not memory in bytes? Add `--jump-mode relative --jump-ratio 0.3`.

## Evaluate and tune on your data (no code)

In the web app: **Evaluate my data** → upload or pick a ready-made dataset (online shop, NAB cloud / Twitter / ads / traffic / machines) → read the report → **Use these settings for my live queue** → **Show this data in the queue**.

Same engine from the command line:

```bash
python -m product.cli evaluate --metrics orders.csv --incidents incidents.csv --events notifications.csv --budgets 5,10,20
python -m product.cli tune     --metrics orders.csv --incidents incidents.csv   # also saves the settings for the app
python ../tools/fetch_nab.py                                                     # optional: public NAB data (MIT)
```

## Run the web app

```bash
uvicorn product.app:app --port 8080
```

Open http://127.0.0.1:8080 → **Run sample**, or upload your CSVs. **Run sample** builds one synthetic day (a memory jump, a monitoring gap, a slow leak, and a few log lines) and scores it with the built-in rules, so the queue is not alerts-only. Mark items **Real / Noise**; the next run that has enough labels trains on the marks. **Download confirmed faults** exports them as `faults.csv`. A short script for an instructor demo is [../../presentation/VIVA_ANSWERS.md](../../presentation/VIVA_ANSWERS.md).

Docker (bound to localhost; set `APP_PASSWORD` before sharing — behind a reverse proxy every request looks local, so the private-network guard alone does not protect it):

```bash
docker compose up --build
```

That builds the CPU image (`Dockerfile`). A CUDA image is optional and is not what CI builds:

```bash
docker compose --profile gpu up --build oncall-queue-gpu
```

## Connect it to your stack

| What | How |
|---|---|
| Alertmanager | Webhook receiver → `POST /ingest/alertmanager`. Set `INGEST_TOKEN` and use `http_config.authorization.credentials`. |
| Prometheus | `PROM_URL=http://prometheus:9090` (default query `container_memory_working_set_bytes`; override with `PROM_QUERY`, map services with `PROM_SERVICE_LABEL`). |
| Slack / Opsgenie | Save a webhook URL or API key on the same panel (or `SLACK_WEBHOOK_URL`, `OPSGENIE_API_KEY`). Same gate and the same items as PagerDuty; Slack gets one message per job, Opsgenie one alert per item. |
| PagerDuty | Save a routing key and switch to **Live** on the Connectors panel (type `LIVE`; key required). Only **ingest** jobs page; sample / GAIA / Evaluate never page. Env `PAGERDUTY_ROUTING_KEY` + `SHADOW_MODE=0` are bootstrap defaults. |

Full guide: [product/CONNECTORS.md](product/CONNECTORS.md).

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PAGE_LOOKBACK_MINUTES` | `60` | First ingest job only: how far back metric items may be paged (later jobs page only what is new since the last one) |
| `SHADOW_MODE` | `1` | Bootstrap default; UI Live override is saved in `ops.json`. `0` = Live |
| `APP_PASSWORD` / `APP_USER` | none / `oncall` | Operator login. `POST /login` sets an HttpOnly session cookie; basic auth still works for scripts. Required before Live mode, and behind a reverse proxy |
| `APP_VIEWER_PASSWORD` / `APP_VIEWER_USER` | none / `viewer` | Read-only login. Writes (Live, keys, settings, jobs) return 403 |
| `COOKIE_SECURE` | off | `1` marks the session cookie Secure. Also set when a proxy in `TRUSTED_PROXIES` sends `X-Forwarded-Proto: https` |
| `AUDIT_LOG_PATH` | `audit.log` beside the ops file | Extra copy of the audit log. `off` keeps stdout only |
| `SECRETS_PATH` / `SECRETS_DIR` | `secrets.json` beside the ops file / `/run/secrets` | Paging keys. A saved key is not written into `ops.json` |
| `INGEST_TOKEN` | none | Bearer token for `/ingest/*` (header only; `?token=` is refused) |
| `TRUSTED_PROXIES` | none | CIDRs of reverse proxies. Only then is `X-Forwarded-For` / `Forwarded` read, from the right: the first hop that is not a listed proxy is the client for the login lock, ingest rate limit and audit log. `X-Forwarded-Proto` is honoured only from these proxies |
| `ALLOWED_HOSTS` | none | Extra `Host` names the app answers from this machine without a password (DNS-rebinding guard) |
| `MAX_INGEST_MB` | `10` | Largest JSON body `/ingest/*` and other JSON routes accept; bigger bodies get 413 while they stream in |
| `INGEST_MAX_ALERTS` | `400` | Largest number of alerts in one ingest POST; bigger batches get 413 |
| `INGEST_MAX_BATCHES` / `INGEST_WINDOW_SECONDS` | `60` / `60` | Ingest POSTs per client in the window; over the cap gets 429 |
| `MODEL_CANDIDATE_KEEP` | `20` | Refused models whose file is kept; older candidate files are deleted (their cards stay). Versions that were ever active are never deleted |
| `PAGE_KEY_MAX` | `10000` | Oldest paging keys (by item start time) are dropped so `ops.json` cannot grow without bound; keys of items still in the history are always kept, so nothing is paged twice |
| `AUDIT_LOG_MAX_BYTES` | `1000000` | Audit file is rotated to `audit.log.1` when it grows past this size |
| `LOG_FORMAT` | text | `json` writes one JSON object per log line (stdout and the audit file) |
| `INGEST_DEDUP_HOURS` | `24` | An alert with the same content seen within this window is not buffered again |
| `PROM_ALLOW_PRIVATE` | `0` | `1` allows a private / loopback Prometheus (in-cluster); metadata addresses are always refused |
| `JUMP_MODE` | `absolute` | `relative` for metrics that are not bytes |
| `JUMP_BYTES` | `5e8` | Absolute rise that makes a point hot |
| `JUMP_RATIO` | `0.3` | Relative rise (30% over the recent median) |
| `JUMP_WINDOW_MINUTES` | `15` | Recent-median window |
| `CONFIRM_MIN_RISE` | none | Only show jumps at least this big when confirmed |
| `ALERT_SHARE` | `0.25` | Share of the daily budget log alerts may use |
| `GAP_MINUTES` | `10` | Silence that becomes a gap item |
| `DAY_OFFSET_HOURS` | `0` | Hours added to UTC before cutting on-call days |
| `LEAK_WINDOW_MINUTES` / `LEAK_BYTES` | `180` / `5e8` | Slow-leak rule (byte metrics only); `0` = off |
| `GROUP_MINUTES` | `0` | Metric items this close on other services share one slot |
| `CARRY_OVER_MAX` | `0` | Unused slots carried to the next day (e.g. `10`) |
| `PROMOTION_MIN_SAVING_MISSES` | `1` | Promotion gate margin: a new model must make the held-out queue cheaper than both alternatives by at least this many missed faults (× the miss cost of 10); `0` = any saving |
| `MAX_UPLOAD_MB` | `200` | Largest CSV the upload and Evaluate pages accept (the whole request is stopped while it streams in once it passes 3 files' worth) |
| `JOB_RETENTION` / `EVAL_RETENTION` | `200` / `30` | Jobs and evaluations kept on disk; older ones are deleted (Real / Noise marks are kept) |
| `ALLOW_OPEN_ACCESS` | unset | Without a password the app only answers private networks; `1` lifts that |
| `SETTINGS_PATH` | `product/data/settings.json` | Settings saved from **Evaluate my data** / `cli tune` (replace the built-in rule) |
| `AUTH_DB` | `auth.db` beside the ops file | Sign-in sessions (tokens stored as SHA-256), the login lock and ingest rate counters; kept across restarts |
| `OPS_PATH` | `product/data/ops.json` | Persisted Shadow/Live override + saved PagerDuty routing key |
| `QUEUE_DB`, `MODEL_PATH` | `product/data/…` | Where state is kept (the model gets a `.sha256` file; a changed file is refused) |

## How it decides

- **No labels (day one):** rules only — jumps, gaps, log severity with duplicate suppression.
- **With labels or marks:** a cost-sensitive classifier (a miss weighs 10× a false alarm) is trained on the early window and tested on the rest. It ranks log alerts only if it passes the promotion gate there: it must beat showing no log alerts, alert by alert and in the whole queue, save at least one missed fault's worth of queue cost over both no log alerts and what would run instead (`PROMOTION_MIN_SAVING_MISSES`), and be no worse than the active model. Otherwise it is kept as a candidate and rules only (or the active model) scores the batch; on GAIA this is what happens. An active model is reused for small webhook batches; the page reminds you to retrain after 7 days or new Real marks.
- **Always causal:** nothing is chosen using data from later in the day.

---

## Reproduce the course results (GAIA)

1. Download GAIA MicroSS: `python ../tools/fetch_gaia.py` fetches https://github.com/CloudWise-OpenSource/GAIA-DataSet (about 1.8 GB), extracts only the files used and checks each SHA-256.
2. It puts the run tables at `data/gaia_raw/run/run` and the memory csv files at `data/gaia_metrics/metric`.
3. Run:

```bash
python -m pytest -q
python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run --sweep   # jump size, week 1
python run_queue_eval.py      # headline table, K = 5/10/20/50
python run_classifier_ablation.py  # what the alert classifier adds inside the queue
python run_walkforward.py     # 8 later weeks, everything re-learned each week
python run_stats.py           # intervals, sign tests, signals, chance overlap, confirm time
python run_robustness.py      # synthetic 0.25-2 GB steps, spikes, ramps, leaks on the real series
python ../tools/fetch_nab.py && python run_nab.py   # other domains (NAB)
python ../tools/fetch_smd.py && python run_smd.py   # other fault types: 28 real servers, CPU/memory/disk/network (SMD)
python make_figures.py        # redraw ../figures/*.svg from the csv files
python run_validate.py        # fails if the docs and the csv files disagree
```

Every system in the table goes through the same code (`src/experiment.py`, `src/queue.py`). See [../VALIDATION.md](../VALIDATION.md).

Older alert-only experiments live in `experiments/legacy_alerts_only/` (`run_m1`, `run_m2`, `run_m4`, `run_stretch`, `run_memory_clf`; run with `python -m experiments.legacy_alerts_only.run_m1`). They score log alerts only; do not quote them as the system result.

## Layout

| Path | Purpose |
|---|---|
| `src/memory_spikes.py` | Jumps (absolute / relative), slow leaks, gaps, old mean + 3σ rule |
| `src/jump_ranker.py` | Phase 3 learned jump ranker (evaluated; not used by default) |
| `src/baselines_metric.py` | Static and rate (`delta`) rules — the fair baselines |
| `src/queue.py` | Causal queue, scorer, oracle ceiling, confirm-threshold tuning |
| `src/experiment.py` | One window, every system — used by all GAIA results scripts |
| `src/generic.py` | The same for any data: column detection, tuning, report (web app, CLI, `run_nab.py`) |
| `product/datasets.py` | Ready-made datasets for the Evaluate page (shop example, NAB download) |
| `src/classifier.py`, `src/policy.py`, `src/features.py` | Alert classifier and cost policy |
| `src/eval_stats.py` | Bootstrap CI, sign test, signal counts, chance overlap |
| `src/gaia_loader.py` | GAIA run tables → alerts + faults (UTC) |
| `product/` | Web app, CLI, store, connectors |
| `examples/` | Synthetic export for the quick start |
| `../tools/build_rehearsal.py`, `../tools/md2docx.js` | Rebuild the slide deck and the Word copies of the Markdown docs |
| `tests/` | 514 tests: 504 pass on CPU CI, 9 skip without torch, xgboost or CUDA, and `tests/integration` runs only with the Docker stack (`python -m pytest -q`) |
