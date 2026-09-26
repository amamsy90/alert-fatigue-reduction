# On-call queue — budgeted incident detection from metrics

Your services raise more alerts than one person can read. This tool gives on-call **at most K items a day**, in the order they happen:

1. **Metric jumps** — a service's metric suddenly rising well above its own last 15 minutes (e.g. +0.5 GB of memory, or +30% of anything in relative mode). Shown ~30 s after the jump starts.
2. **Monitoring gaps** — a service stopped sending metrics for 10 minutes. You are blind there; that is worth knowing.
3. **Log alerts** — ranked by severity (no labels needed) or by a model trained on your Real / Noise marks. They may use at most a quarter of the slots, and an alert inside a shown jump rides on it instead of taking a slot.

It runs in **shadow mode** by default: nothing is paged until you turn it on.

On the public GAIA benchmark it catches **71–73%** of injected faults at 20 items a day (the best possible is 85%), against **69%** for a Prometheus `delta()` rule and **0%** for log severity, with half the false items. Details: [../FINAL_REPORT.md](../FINAL_REPORT.md).

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

## Run the web app

```bash
uvicorn product.app:app --port 8080
```

Open http://127.0.0.1:8080 → **Run sample**, or upload your CSVs. Mark items **Real / Noise**; the next run trains on the marks. **Download confirmed faults** exports them as `faults.csv`.

Docker (bound to localhost; set `APP_PASSWORD` before sharing):

```bash
docker compose up --build
```

## Connect it to your stack

| What | How |
|---|---|
| Alertmanager | Webhook receiver → `POST /ingest/alertmanager`. Set `INGEST_TOKEN` and use `http_config.authorization.credentials`. |
| Prometheus | `PROM_URL=http://prometheus:9090` (default query `container_memory_working_set_bytes`; override with `PROM_QUERY`, map services with `PROM_SERVICE_LABEL`). |
| PagerDuty | `PAGERDUTY_ROUTING_KEY=…` and `SHADOW_MODE=0`. Escalated alerts, shown jumps and gaps are paged with stable dedup keys. |

Full guide: [product/CONNECTORS.md](product/CONNECTORS.md).

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SHADOW_MODE` | `1` | `0` = page PagerDuty |
| `APP_PASSWORD` / `APP_USER` | none / `oncall` | Basic-auth login for everything except `/health` |
| `INGEST_TOKEN` | none | Bearer token for `/ingest/*` |
| `JUMP_MODE` | `absolute` | `relative` for metrics that are not bytes |
| `JUMP_BYTES` | `5e8` | Absolute rise that makes a point hot |
| `JUMP_RATIO` | `0.3` | Relative rise (30% over the recent median) |
| `JUMP_WINDOW_MINUTES` | `15` | Recent-median window |
| `CONFIRM_MIN_RISE` | none | Only show jumps at least this big when confirmed |
| `ALERT_SHARE` | `0.25` | Share of the daily budget log alerts may use |
| `GAP_MINUTES` | `10` | Silence that becomes a gap item |
| `DAY_OFFSET_HOURS` | `0` | Hours added to UTC before cutting on-call days |
| `QUEUE_DB`, `MODEL_PATH` | `product/data/…` | Where state is kept |

## How it decides

- **No labels (day one):** rules only — jumps, gaps, log severity with duplicate suppression.
- **With labels or marks:** a cost-sensitive classifier (a miss weighs 10× a false alarm) ranks log alerts; the escalate threshold is learned on the early window. A model is saved and reused for small webhook batches; the page reminds you to retrain after 7 days or new Real marks.
- **Always causal:** nothing is chosen using data from later in the day.

---

## Reproduce the course results (GAIA)

1. Download GAIA MicroSS: https://github.com/CloudWise-OpenSource/GAIA-DataSet
2. Put the run tables at `data/gaia_raw/run/run` and the memory csv files at `data/gaia_metrics`.
3. Run:

```bash
python -m pytest -q
python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run --sweep   # jump size, week 1
python run_queue_eval.py      # headline table, K = 5/10/20/50
python run_walkforward.py     # 8 later weeks, everything re-learned each week
python run_stats.py           # intervals, sign tests, signals, chance overlap, confirm time
python run_validate.py        # fails if the docs and the csv files disagree
```

Every system in the table goes through the same code (`src/experiment.py`, `src/queue.py`). See [../VALIDATION.md](../VALIDATION.md).

Older alert-only experiments (`run_m1.py`, `run_m2.py`, `run_m4.py`, `run_stretch.py`, `run_memory_clf.py`) score log alerts only; do not quote them as the system result.

## Layout

| Path | Purpose |
|---|---|
| `src/memory_spikes.py` | Jumps (absolute / relative), gaps, old mean + 3σ rule |
| `src/baselines_metric.py` | Static and rate (`delta`) rules — the fair baselines |
| `src/queue.py` | Causal queue, scorer, oracle ceiling, confirm-threshold tuning |
| `src/experiment.py` | One window, every system — used by all results scripts |
| `src/classifier.py`, `src/policy.py`, `src/features.py` | Alert classifier and cost policy |
| `src/eval_stats.py` | Bootstrap CI, sign test, signal counts, chance overlap |
| `src/gaia_loader.py` | GAIA run tables → alerts + faults (UTC) |
| `product/` | Web app, CLI, store, connectors |
| `examples/` | Synthetic export for the quick start |
| `tests/` | 62 tests (`python -m pytest -q`) |
