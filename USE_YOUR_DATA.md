# Use it on your own data

You do not need to write code, and your data does not have to come from microservices. Anything that is **a number over time** works: orders per minute, CPU, sensor temperature, sign-ups, notification or ticket counts, ad clicks, queue length, payment failures.

The app answers one question honestly: *if a person could look at only K items a day, which items should they see — and would that have caught my past problems?*

---

## 1. Start the app

```powershell
cd app
pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
uvicorn product.app:app --port 8080
```

Open **http://127.0.0.1:8080/evaluate** (or click **Evaluate my data** on the queue page). With Docker: `docker compose up --build`, same address.

## 2. Try a ready-made dataset first (1 minute)

Click **Evaluate this** on one of the cards:

| Dataset | Domain | Where from |
|---|---|---|
| Online shop: orders per region + notifications | e-commerce | synthetic (`examples/make_business_example.py`) |
| NAB realTweets | Twitter mention volume (notification-like stream) | [Numenta Anomaly Benchmark](https://github.com/numenta/NAB), MIT |
| NAB realAWSCloudwatch | Cloud servers | NAB |
| NAB realAdExchange | Online advertising | NAB |
| NAB realTraffic | Road traffic sensors | NAB |
| NAB realKnownCause | Machine temperature, NYC taxi demand, CPU, latency | NAB |
| Microservice memory + alerts | IT | synthetic |

NAB cards say **Download** the first time (it needs internet; about 15 MB).

## 3. Prepare your own files

Only the first file is required. Column names are detected; these all work.

**Metrics** — one row per measurement:

| time | orders | region |
|---|---|---|
| 2026-02-01 10:00 | 118 | eu |
| 2026-02-01 10:05 | 121 | eu |

Also accepted: `timestamp,value,service`, Unix seconds or milliseconds, or one column per series (`time, cpu_web, cpu_db, …`).

**Past problems** (optional, but needed for a score) — from your incident log, postmortems, or support escalations:

| start | end | region |
|---|---|---|
| 2026-02-04 14:45 | 2026-02-04 15:10 | us |

No `end`? Each problem gets 10 minutes (change it on the page). No series column? A problem applies to every series.

**Alerts / notifications** (optional) — alerts, tickets, push notifications, log lines:

| time | region | level | text |
|---|---|---|---|
| 2026-02-04 14:52 | us | error | ticket: payment failed |

## 4. Read the result

The app uses the **first 30%** of each series to tune (change it on the page) and scores the rest once. It compares, all under the same budget K:

- **Your alerts by severity** — what you do today;
- **Static threshold** — "value above its usual range";
- **Rate rule** — "sudden change from a few points ago" (like a Prometheus `delta()` alert);
- **Jump rule** — "far from its own recent level", in robust units, so it works for any metric;
- **Your queue** — the detector that did best on the tuning part, plus monitoring-gap items and your alerts.

Check two numbers before you trust it:

1. **The grey line (random chance).** If your problems are long compared with the gaps between items, even random items overlap them. A result is only good if it is well above that line.
2. **Past problems used to tune.** With fewer than about five, the settings are a guess; add older incidents or raise the tuning share.

"Why problems were missed" splits misses into *no data* (your monitoring was down — worth fixing on its own), *not detected*, and *budget full* (raise K, or turn on carry-over).

## 5. Use it

- **Use these settings for my live queue** saves the tuned detector (`product/data/settings.json`). The queue page, the Alertmanager webhook and `cli score` then use it.
- **Show this data in the queue** scores your files day by day so you can mark items **Real / Noise**. Marks become labels for the next training.
- **Go back to the built-in rule** removes the saved settings.

Then run in **shadow mode** (the default) for two weeks, marking items, before you turn on paging (`SHADOW_MODE=0` + `PAGERDUTY_ROUTING_KEY`).

## Command line (same engine)

```powershell
python -m product.cli evaluate --metrics orders.csv --incidents incidents.csv --events notifications.csv --budgets 5,10,20
python -m product.cli tune     --metrics orders.csv --incidents incidents.csv      # also saves the settings
python -m product.cli score    --metrics orders.csv --alerts notifications.csv --out queue.csv
```

## What it measured on data that is not microservices

Tuned on the first half of each series, scored on the second half, K = 2 items a day (`app/run_nab.py`, `app/results/nab_eval.csv`):

| Dataset | Test problems | Your queue caught | Random chance | Items / day |
|---|---|---|---|---|
| Online shop (synthetic, K = 5, 30% tuning) | 11 | 10 (91%) | 1% | 1.3 |
| NAB cloud servers | 17 | 12 (71%) | 58% | 0.3 |
| NAB online ads | 6 | 5 (83%) | 58% | 1.4 |
| NAB machine / taxi / CPU | 13 | 5 (38%) — static rule 54% | 15% | 0.04 |
| NAB road traffic | 11 | 5 (45%) | 13% | 0.4 |
| NAB Twitter volume | 11 | 4 (36%) | 36% | 1.7 |

It helps clearly on the shop data, road traffic and machine data, a little on cloud servers and ads (NAB's wide labels make chance high there), and **not at all on Twitter volume**. That is the point of the page: it tells you on *your* data, before you rely on it.
