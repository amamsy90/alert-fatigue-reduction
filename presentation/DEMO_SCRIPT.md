# 5-minute demo script (viva)

App: http://127.0.0.1:8080  
From `app/`: `docker compose up -d --build` (shadow mode), or `uv run uvicorn product.app:app --port 8080`  
Numbers below match `../FINAL_REPORT.md` / `run_validate.py` (not the older `../CODE_REVIEW.md`).

---

## 0:00–0:30 — One sentence

"This is a budgeted on-call queue: at most K items a day, causal, preferring metric jumps and monitoring gaps over log spam. No GPT in the runtime."

---

## 0:30–2:00 — Run sample (show the product)

1. Open the queue page → **Run sample**.
2. Point at the **jump**, **gap**, and **leak** cards (metric filter if needed).
3. Say: "One click builds a synthetic day so you see the three metric item types. This is demo data, not the GAIA proof."

---

## 2:00–3:30 — Evaluate my data + ML on metrics

1. Open **Evaluate my data**, then click **Evaluate this** on the **Online shop** card (synthetic data).
2. Show the table: severity vs static / rate / jump / your queue.
3. Point at **learned jump level per series**.
4. Say: "Two ML pieces. Log alerts: cost-sensitive classifier from Real/Noise marks. Metrics: each series learns its confirm rise on the train window only, then that number is frozen. ChatGPT is not in this path."

---

## 3:30–4:30 — GAIA headline (slide or report)

Say roughly:

- Public proof: GAIA MicroSS, train week 1, test 55 days, 1,223 faults.
- At K = 20: the detector alone (jump rule + learned threshold) **72.6%** (86% of the 84.9% ceiling, 1.0 false items a day) vs the Prometheus-style rate rule **68.9%** (2.1 false items a day) and log severity **0%**. The shipped configuration (the promotion gate refuses the classifier on GAIA, so log alerts are chosen by severity) catches **71.0%** (84% of the ceiling, 1.4 false items a day); the same queue with the classifier's alerts would catch 69.6% (1.8 false items a day), which is why the gate keeps it out.
- Walk-forward mean **71.3%** (95% CI 64.5–76.7%); the detector alone 74.8%, 8/8 weeks over the rate rule.
- Misses: mostly no metric data or a full budget — not "silent faults."

Optional one line: "After removing the last look-ahead the full queue ties the rate rule; the detector alone is the claim that holds (8/8 weeks)."

---

## 4:30–5:00 — Why not GPT / what's new

- **Why not GPT:** live budget K, causal decisions, reproducible baselines, local telemetry — not a chat paste.
- **What's new:** budgeted causal metric queue + honest baselines + Evaluate-any-CSV + per-series learned confirm levels.

Closer: "GPT writes text; this decides which K items on-call should see today."

---

## If they interrupt

| Question | Point them at |
|---|---|
| What data? | `VIVA_ANSWERS.md` §1 — GAIA / NAB / synthetic / your CSV |
| Where is GPT? | Nowhere in runtime — rules + sklearn + per-series thresholds |
| Why the web app? | Connectors, shadow mode, budget K, measurable rates |
| Is sample the result? | No — sample is demo; headline is GAIA table in the report |

---

## Pre-flight (day of viva)

```text
cd app
uv run pytest -q
uv run python run_validate.py
docker compose up -d --build
```

Expect: **300 passed, 1 skipped** (the skipped integration test needs Docker), **VALIDATION OK (195 numbers)**, and `docker compose ps` showing the container `healthy`.

Also, before the viva, click **Go back to the built-in rule** on the Evaluate page and delete `app/product/data/model.pkl` (if present). Otherwise the queue and any live alert are scored with the detector and classifier learned from the last data you evaluated (e.g. the online shop), and a "critical" sample alert can come out as *hide* — correct for that model, confusing in a demo.

Checked in Docker on 14 Oct 2026: Run sample, Evaluate this (Online shop), Use these settings for my live queue, Show this data in the queue, Go back to the built-in rule, and `POST /ingest/alertmanager` in shadow mode — all pass. Run sample failed in Docker before that day's Dockerfile fix (`/app/data/sample_product` was not writable).
