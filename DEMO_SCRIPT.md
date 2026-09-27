# 5-minute demo script (viva)

App: http://127.0.0.1:8080  
From `app/`: `uvicorn product.app:app --port 8080`  
Numbers below match `FINAL_REPORT.md` / `run_validate.py` (not the older `CODE_REVIEW.md`).

---

## 0:00–0:30 — One sentence

"This is a budgeted on-call queue: at most K items a day, causal, preferring metric jumps and monitoring gaps over log spam. No GPT in the runtime."

---

## 0:30–2:00 — Run sample (show the product)

1. Open the queue page → **Run sample**.
2. Point at the **jump**, **gap**, and **leak** cards (metric filter if needed).
3. Say: "One click builds a fake day so you see the three metric item types. This is demo data, not the GAIA proof."

---

## 2:00–3:30 — Evaluate my data + ML on metrics

1. Open **Evaluate my data** → pick **Online shop**.
2. Show the table: severity vs static / rate / jump / your queue.
3. Point at **learned jump level per series**.
4. Say: "Two ML pieces. Log alerts: cost-sensitive classifier from Real/Noise marks. Metrics: each series learns its confirm rise on the train window only, then that number is frozen. ChatGPT is not in this path."

---

## 3:30–4:30 — GAIA headline (slide or report)

Say roughly:

- Public proof: GAIA MicroSS, train week 1, test 55 days, 1,223 faults.
- At K = 20: full queue **70.2%** (about **83%** of the 84.9% ceiling), vs Prometheus-style delta **~69%**, log severity **0%**.
- Walk-forward mean **72.5%** (95% CI 65.7–78.3%).
- Misses: mostly no metric data or a full budget — not "silent faults."

Optional one line: "Carry-over stretch reaches 74.4%; I keep the honest 70.2% as the reference claim."

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
python -m pytest -q
python run_validate.py
uvicorn product.app:app --port 8080
```

Expect: **140 passed**, **VALIDATION OK**.

Also, before the viva, click **Go back to the built-in rule** on the Evaluate page and delete `app/product/data/model.pkl` (if present). Otherwise the queue and any live alert are scored with the detector and classifier learned from the last data you evaluated (e.g. the online shop), and a "critical" sample alert can come out as *hide* — correct for that model, confusing in a demo.

Checked from a fresh clone on 27 Sep 2026: `pip install`, quick start, Run sample, Evaluate (online shop), Use these settings, Show in queue, Alertmanager webhook — all pass.
