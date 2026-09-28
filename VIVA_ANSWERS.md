# Viva answers

Say these in your own words. The numbers are the ones in `FINAL_REPORT.md` (checked against `app/results/`). `CODE_REVIEW.md` is the earlier review that changed the project; some of its percentages (for example 72.8% and 68.8%) are the old run, before the causal queue. Quote the report, not the review.

There is no language model in this product. A search of the code finds no GPT, OpenAI, or other LLM client.

---

## 1. What data is used?

Four different sets, and they are not the same thing.

**GAIA MicroSS** is the public proof. CloudWise, July–August 2021: 34,863 log lines and 1,367 injected faults. Every fault is the same program: it holds 1 GB of extra memory for 600 seconds on one service. The first 7 days are the training window. The next 55 days are the test: 1,223 faults. Days are never shuffled. That is the headline table in the report.

**NAB** is an external check, not a second microservice benchmark. Five public domains from the Numenta Anomaly Benchmark: AWS CloudWatch, online ads, a mixed set (machine temperature, taxi demand, CPU), road traffic, and Twitter volume. Each series is tuned on its first half and scored on the second half, at 2 items a day. The table is in the report, section 9, and in `app/results/nab_eval.csv`.

**Synthetic examples** are for the demo, not for the headline.

- **Run sample** on the queue page builds one fake on-call day: a memory jump, a monitoring gap, a slow leak, and a few log lines. It is generated in `src/sample_data.py` (`generate_demo_day`). It is not GAIA.
- **Online shop** on the Evaluate page is 21 days of fake orders in three regions, plus a notification stream. That is `examples/make_business_example.py`.
- The tiny CLI example (`examples/make_example.py`) is another one-day memory file, for the command line.

**Evaluate my data** is the path for someone else's CSVs. Any timestamp and value, long or wide. Optional alerts or notifications, optional past incidents. The app finds the columns, tunes on the early part, and scores the later part. No code.

The old `generate()` function still writes two weeks of alerts with no metrics. Only the alerts-only scripts use it (`run_m1.py` and the classifier smoke test). Do not quote those scripts as the system result. The report says the same thing in section 11.

---

## 2. Where is the GPT?

There isn't one.

Detection starts as rules on the metric stream:

- a **jump** when a point is more than 0.5 GB above the median of the previous 15 minutes on the same series (two hot points confirm it);
- a **slow leak** when the same idea is run on a 3-hour median (on for byte metrics, off if `LEAK_WINDOW_MINUTES` is 0);
- a **monitoring gap** when a series has no point for 10 minutes.

The machine learning on the **metrics** is not a neural net. When the batch has a training window and past faults, each series with enough of its own training jumps gets a **confirm level** (how big the rise must be before the queue shows it). That level is chosen on the early window only, with the same cost as the old single threshold: a miss weighs 10 times a false item. It is then frozen. A series with too little training signal keeps the global level. The queue is still first-come. Nothing is re-fit on the test window, and a later point cannot move the number. Evaluate my data shows the map as "learned jump level per series". Run sample does not learn one: that day has no later window, on purpose.

The published GAIA table uses the **single** threshold for every service (0.62 GB on week 1); `config.confirm_per_series` is off in the experiment scripts. The per-series version is its own row (`p3_per_series` in `results/queue_eval_gaia.csv`): 70.0% at K = 20 with fewer false items, but 84.1% instead of 85.9% at K = 50, because some series' levels reject real faults. That is why it is an option, not the headline.

Log alerts are separate. With no labels they are ranked by severity. If there are Real/Noise marks, or a faults file, an optional **scikit-learn logistic regression** can take the alert share of the budget. It is trained only on the early window, with a miss costing 10 times a false alarm. It is not a language model, and it does not decide the metric jumps. Run sample does not train it: that day has fewer than 10 alerts.

A model may have helped write this code offline. Nothing in the running app calls one. No prompt, no API key, no chat completion. The queue does not ask a model what to page.

---

## 3. Why this app, and not just ask GPT?

GPT is a chat box. This is an on-call tool.

- It can sit on a stream. Alertmanager posts webhooks, Prometheus can be queried, and the queue is rebuilt as data arrives. A chat session does not run all day against Prometheus.
- There is a hard daily budget K. At most K items, first come first served. Log alerts may use at most a quarter of the slots, and gap items at most a quarter, so a noisy log cannot push out a jump that arrives later. You cannot get that guarantee by pasting a day's logs into a chat.
- Decisions are causal. A jump is shown at the point that confirms it, using only the rise known by then. The code does not rank the day by the final peak. A chat answer that has already seen the whole day is look-ahead.
- The numbers are reproducible. The same scripts write `app/results/`, and `run_validate.py` fails if the report and the CSVs drift. You can walk forward, re-learn only from the past, and compare with a Prometheus-style `delta()` rule and a static threshold. A chat reply is not that experiment.
- Private ops telemetry should not go into a chat box. This app runs on your side, in shadow mode by default, and only pages (PagerDuty, Slack, or Opsgenie) if you turn Live on, type LIVE to confirm, and configure at least one channel.

Use GPT to explain the code or draft slides. Do not use it as the detector.

---

## 4. What's new?

The contribution is a **budgeted causal queue**, not "we ran a classifier on logs."

- Metric jumps and monitoring gaps come first. Log spam only fills a reserved share of K. An alert that lands inside a jump that is already shown rides on that jump and does not take another slot.
- The comparison is honest. On GAIA at K = 20 the log-severity rule catches **0%**, because most faults never write a log. A static "memory above the week-1 99th percentile" rule catches **57.2%**. A Prometheus-style `delta(mem[5m]) > 0.5 GB` rule catches **68.9%**. The jump rule plus a threshold learned on week 1 catches **72.6%** (1.0 false item a day, 94% precision). The full queue, which also spends slots on leaks, gaps and a few alerts, catches **69.6%** (1.8 false items a day, 90% precision). That is **82%** of the best possible at K = 20, which is **84.9%**, because GAIA injects about 22 faults a day. Beating a 0% log rule would have been a straw man. The report says the full queue is a bit below the detector alone on this dataset exactly because leaks, gaps and alerts spend slots, and every GAIA fault is a 1 GB memory fault.
- **Evaluate my data** scores any time series without writing code, and shows a chance line.
- **Gap items** turn "the metric file was empty" into something on-call can see. On GAIA, 46% of the full queue's misses are monitoring outages. The gap does not count as catching the fault. It says we are blind.

What is not new, if they push: the jump rule is a threshold on a rolling median. The modest part is beating the rate rule by a few points (the detector wins 8 of 8 walk-forward weeks; the full queue's weekly edge is not significant at 5%). The useful part is the budget, the real-time rule, fewer false items, leaks, and gaps.

---

## Demo script

About five minutes. Defaults: shadow mode on, K = 20, leak rule on, no saved Evaluate settings.

1. Start the app from `app/` (`uvicorn product.app:app --port 8080`) and open the queue page.
2. Click **Run sample**. That one click builds a short synthetic day and scores it. The job has metrics. You should see three cards: **Memory jump**, **Monitoring gap**, **Slow leak**, each with a count of at least one.
3. Point at them in confirmation order, not "biggest at the end of the day":
   - **checkout**, about **+1.2 GB**, shown at 09:00. Two log lines sit inside that window. The error line says it is inside a shown memory jump, so it does not take its own slot.
   - **payments** gap: the series goes silent around 14:00. The card says monitoring gap, the row says no data.
   - **cart** slow leak: memory climbs about 1 GB over two hours from 12:00. The 15-minute rule does not call that a jump. The 3-hour rule does. The badge says **Slow leak**.
   - A few other log lines are noise (cache refresh, heartbeat, a gateway timeout that is not a metric fault). Use the metric filter if you want only jumps, only gaps, or only leaks. The escalate filter on the log table does not hide the cards.
4. Open **/evaluate**. Under ready-made datasets, pick **Online shop: orders per region + notifications** and click **Evaluate this**. When the table appears, find **Your queue** and **Your alerts / notifications by severity**. The stored result in the report is about **91%**: 10 of 11 test problems, against a chance line of about **1%**, at K = 5. The notification stream by severity catches **3 of 11**. The page recomputes it, so you may see 90.9% rather than 91. Say "about 91%."
5. Then say the GAIA headline, from the report, even if the GAIA files are not on this machine (Run GAIA needs them; Run sample does not):
   - At 20 items a day the full queue catches **69.6%** of the 1,223 test faults. That is 82% of the 84.9% ceiling. About **1.8** false items a day, precision **90%**.
   - The Prometheus-style rate rule catches **68.9%**. Log severity catches **0%**. The jump detector on its own, with the week-1 threshold, catches **72.6%**.
   - Where the metric data actually exists, the jump overlaps **99.6%** of faults (1,053 of 1,057). Median time to confirm is **0.72 minutes**.
   - Walk-forward, re-learned from the past only: detector **74.8%** (wins 8/8 weeks), full queue **71.3%** (ties the rate rule week by week), rate rule **70.7%**. The weak week is a monitoring outage (only 61% of that week's faults have any metric data), and the queue shows it as a gap.
6. If they ask "does it only work because every fault is 1 GB?": on the real series, steps from 0.5 GB to 2 GB and one-minute spikes are caught at least 98% of the time by the jump rule; 0.25 GB is not; slow leaks need the leak rule (about 99% of 30-minute ramps, about 73% of 3-hour leaks). NAB is the check that it is not only microservices, and on Twitter volume it is no better than chance. Say that. It is in the report.

Do not claim a GPT, do not claim the full queue beats the detector alone on GAIA, and do not quote `run_m1` / `run_m2` / `run_m4`.
