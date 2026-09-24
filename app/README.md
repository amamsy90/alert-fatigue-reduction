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
2. Put the MicroSS run folder at `data/gaia_raw/run/run` and memory csv files at `data/gaia_metrics`.
3. Run (this is the path used for all published numbers):

```powershell
python run_m1.py --gaia-run data/gaia_raw/run/run
```

## Tests

```powershell
pytest
```

## Layout

| Path | Purpose |
|---|---|
| `src/entities.py` | Alert / Fault / Decision data model |
| `src/config.py` | Budget K, cost ratio, thresholds, split day, seed |
| `src/ingestion.py` | GAIA loading, alert-to-fault alignment, time split |
| `src/baseline.py` | Fixed thresholds + exact dedup (the benchmark) |
| `src/memory_spikes.py` | Memory jumps (0.5 GB over the last 15 minutes) |
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

A jump is memory rising more than 0.5 GB above the median of the previous 15 minutes, for at least two points. The 0.5 GB size is chosen on week 1 only (`--sweep`). Use `--rule jump` for the published 86.1% figure. A csv is used only when its name contains the service and "memory".

## Run M4 (decision policy)

```powershell
python run_m4.py --gaia-run data/gaia_raw/run/run
python run_m4.py --sample
```

Chooses an escalate threshold on week 1, then spends each daily budget on week 2. Results land in `results/m4_gaia.csv`. **Alerts only** — do not quote this as the system result. The system gate is `run_queue_eval.py`: more faults caught than the baseline at every K.

## Run the stretch (ask a person, budget sensitivity)

```powershell
python run_stretch.py --gaia-run data/gaia_raw/run/run
```

**Alerts only** — does not count silent memory jumps. The system result is `run_queue_eval.py` (72.8% at K=20) and `run_walkforward.py` (mean 74.7% across later weeks). `run_stats.py` adds the 95% interval, the 8/8 test vs baseline, and why most faults have no log. Check headlines with `python run_validate.py`. See [../VALIDATION.md](../VALIDATION.md).

## On-call product (local web app)

See [PRODUCTION_PLAN.md](PRODUCTION_PLAN.md) and [product/README.md](product/README.md).

```powershell
cd app
pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
$env:SHADOW_MODE = "1"
uvicorn product.app:app --reload --port 8083
```

Open http://127.0.0.1:8083 — **Run sample** or **Run GAIA**, set budget K, filter escalate / ask / hide. When memory data is loaded, a **Memory jumps** table lists the biggest K jumps per day with the alerts inside each; a jump with no alerts inside may be a fault the logs missed.

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
