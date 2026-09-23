# Alert Fatigue Reduction — Application

M1 implementation per the design specification: data pipeline, baseline
(fixed thresholds + duplicate suppression), and the full metric harness.

## Setup

```powershell
cd app
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run the M1 smoke test (no GAIA download needed)

```powershell
python run_m1.py --sample
```

Generates two weeks of synthetic GAIA-like data, runs the baseline at budgets
K = 5/10/20/50, and writes `results/m1_sample.csv`.

## Run on the real GAIA data

1. Download the dataset: https://github.com/CloudWise-OpenSource/GAIA-DataSet
2. Place/convert the alert and fault files as `data/gaia/alerts.csv` and
   `data/gaia/faults.csv` (see `src/ingestion.py` docstring for the expected
   columns; adjust `ALERT_COLUMN_MAP` / `FAULT_COLUMN_MAP` to the real files).
3. Run:

```powershell
python run_m1.py --data data/gaia
```

## Tests

```powershell
pytest
```

## Layout

| Path | Purpose |
|---|---|
| `src/entities.py` | Alert / Fault / Incident / Decision data model |
| `src/config.py` | Budget K, cost ratio, thresholds, split day, seed |
| `src/ingestion.py` | GAIA loading, alert-to-fault alignment, time split |
| `src/baseline.py` | Fixed thresholds + exact dedup (the benchmark) |
| `src/memory_spikes.py` | Week-1 memory baseline and spike episodes |
| `src/metrics.py` | Precision/recall, reduction, detection, MTTA, false-alert burden |
| `run_memory.py` | Score spikes against injected faults |
| `src/sample_data.py` | Synthetic GAIA-like generator for smoke tests |
| `run_m1.py` | M1 pipeline runner (D1 deliverable) |

## Run M2 (classifier vs baseline)

```powershell
python run_m2.py --gaia-run data/gaia_raw/run/run
python run_m2.py --sample
```

Trains on week 1 with a missed fault weighed 10x a false alarm, then spends each daily budget on the highest-scoring alerts in week 2. Results land in `results/m2_gaia.csv`.

## Run memory spikes

```powershell
python run_memory.py --sample
python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run
```

Week 1 sets each series' normal memory. Week 2 flags jumps of at least two high points. A csv is used only when its name contains the service and "memory". Extracted `docker_memory_usage_total` files live under `data/gaia_metrics`. `--sample` checks the detector without them.

## Run M4 (decision policy)

```powershell
python run_m4.py --gaia-run data/gaia_raw/run/run
python run_m4.py --sample
```

Chooses an escalate threshold on week 1, then spends each daily budget on week 2. Results land in `results/m4_gaia.csv`. The gate is higher fault detection, MTTA at least 50% lower, and at least 80% fewer false alerts, at every budget.

## Run the stretch (ask a person, budget sensitivity)

```powershell
python run_stretch.py --gaia-run data/gaia_raw/run/run
```

Confident faults keep the daily slots. Scores between the Bayes floor and the week-1 cutoff become a review queue that fills any leftover slots. Writes `results/stretch_gaia.csv` and `results/budget_sensitivity_gaia.md`.

## On-call product (local web app)

See [PRODUCTION_PLAN.md](PRODUCTION_PLAN.md) and [product/README.md](product/README.md).

```powershell
cd app
pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
$env:SHADOW_MODE = "1"
uvicorn product.app:app --reload --port 8080
```

Open http://127.0.0.1:8080 — **Run sample** or **Run GAIA**, set budget K, filter escalate / ask / hide. When memory data is loaded, a **Memory jumps** table lists the biggest K jumps per day with the alerts inside each; a jump with no alerts inside may be a fault the logs missed.

Each alert and jump has **Real / Noise** buttons. Click again to clear. Every later run uses the marks as labels automatically: alerts on the same service inside a Real window (a jump's own window, or ±5 minutes around a Real alert) count as faults, and an alert marked Noise counts as noise even inside a window. The page shows the live precision of what people marked (share of marked jumps and alerts that were real). **Download confirmed faults** exports the Real windows as `faults.csv` for use elsewhere.

Each training run saves the model (`product/data/model.pkl`, or `MODEL_PATH`). Batches too small to train on, such as a few webhook alerts, are scored with the saved model; so are batches that could only use the severity stand-in when the saved model learned from real or confirmed labels. `GET /model` shows its age, and the page recommends retraining after 7 days or when new Real marks arrive; the next full batch (upload or GAIA) retrains it.

Login is off by default for local use. Set a password before anyone else can reach the app (the browser asks for it; `/health` stays open for uptime checks):

```powershell
$env:APP_USER = "oncall"        # optional, this is the default
$env:APP_PASSWORD = "choose-a-long-password"
```

Docker:

```powershell
cd app
docker compose up --build
```

Connectors (webhook + Prometheus): [product/CONNECTORS.md](product/CONNECTORS.md).
