# Shadow-mode pilot plan

**System:** Budgeted Incident Detection · **Status:** ready to run; not yet run

Every result so far comes from public benchmarks (GAIA, NAB, SMD, RCAEval). This plan is the next step: four weeks beside one real on-call team, in shadow mode, so nothing pages. It answers the one question the benchmarks cannot: **on a real team's alerts and metrics, are the items in the queue worth an engineer's time?**

## 1. Who and what

| | |
|---|---|
| Team | One on-call rotation of 3–8 engineers running microservices, with Prometheus and Alertmanager already in place |
| Length | Week 0 (setup and back-test), then 4 weeks in shadow mode |
| Mode | `SHADOW_MODE=1` for the whole pilot. Live paging stays off; every job records what it *would have paged* |
| Data | The team's own alerts (Alertmanager webhook) and memory series (Prometheus). Stays on the team's server (`docs/privacy_responsible_ai.md`) |
| Budget | K = 20 a day to start; the team may change it once, after week 2 |

## 2. Steps

1. **Week 0: back-test before trusting it.** Export 2–4 weeks of the team's past metrics, alerts and incidents, and run them through *Evaluate my data* (or `python -m product.cli evaluate`). Record the queue's catches against the team's current rules and the chance line. If the queue does not beat the chance line, stop here and write down why.
2. **Week 0: install.** One container per `docs/deployment.md`, behind the team's TLS proxy, with `APP_PASSWORD`, `INGEST_TOKEN` and `TRUSTED_PROXIES` set. Point an Alertmanager receiver at `/ingest/alertmanager` and set the Prometheus URL on the Connectors page. Confirm `/health` and `/monitoring`.
3. **Weeks 1–4: mark items.** The on-call engineer opens the queue once a shift and marks each item **Real** or **Noise**. Five minutes a shift is the target.
4. **Weekly 15-minute review.** Compare *would have paged* with what the team's own rules actually paged, and with the incidents the team logged.
5. **After week 4: decide.** Use the criteria in section 3, then go Live, change K or the settings, or stop.

## 3. What is measured, and the bar

| Question | Measure | Where | Bar to go Live |
|---|---|---|---|
| Are queue items worth reading? | Precision from Real / Noise marks | `/monitoring` → `precision_from_marks` | ≥ 0.70 with ≥ 20 marks (the product's own target) |
| Does it catch the team's incidents? | Logged incidents with a queue item before the team's page | Weekly review | At least as many as the team's current rules |
| Is it quieter? | Items a day vs pages a day from the current rules | Job history, Alertmanager | Fewer, at the same or better catch rate |
| Is it early enough? | Minutes from incident start to the queue item | Weekly review | Median no later than the current page |
| Is the input healthy? | Monitoring gaps, alert-volume and per-series metric drift | `/monitoring` | No unexplained warning for more than a day |
| Is it cheap to use? | Engineer minutes a shift spent marking | Ask the team | ≤ 10 |

## 4. Risks and how they are handled

- **Engineers stop marking.** Precision is then unknown, and the pilot result is "not measured", not "pass". Keep marking to one shift a day if needed.
- **The team's faults are not memory faults.** The jump rule works on any numeric series (*Evaluate my data*); add the series the team's incidents show up in during week 0.
- **The alert classifier is not trained yet.** It needs labels of both kinds. Until the promotion gate passes, alerts are ranked by severity, which is the shipped configuration (`docs/report_appendices_full.md` §B1).
- **Personal data in alert text.** Filter it at Alertmanager before it reaches the ingest endpoint; message text is cut to 300 characters either way.

## 5. What is reported

A short note with the table in section 3 filled in, the week-0 back-test, the number of marks, and the decision. It goes in `FINAL_REPORT.md` as a new results section, with its numbers added to `run_validate.py` like every other result.
