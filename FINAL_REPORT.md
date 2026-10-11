# AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices

*(Alert Fatigue Reduction in Cloud Microservice Operations)*

**Final report**

**Author:** Aishah Mohamed Alyammahi

**Course:** Software Engineering and Programming

**Date:** 11 October 2026

Numbers come from scripts under `app/` and are checked by `python run_validate.py` against the *Checked numbers* table at the end. Part A follows the thirteen sections of the course brief; Part B holds the measured results; Part C says how to reproduce them.

| Rubric criterion (weight) | Where it is answered |
|---|---|
| Problem definition and significance (10%) | §2; evidence in Part B §B6 |
| Requirements, users and use cases (10%) | §3, §4; `docs/requirements.md` (20 FR, 11 NFR, 8 user stories, traceability to tests) |
| AI justification and technical approach (10%) | §5, §6 |
| High-level production design (15%) | §6; `docs/architecture.md` (5 diagrams), `docs/adr/` (7 decisions), `docs/threat_model.md` |
| Deployment and scalability (10%) | §7; `docs/deployment.md` |
| Testing strategy (10%) | §8; `docs/test_plan.md`, `.github/workflows/ci.yml` |
| MLOps and lifecycle (20%) | §9; `docs/mlops.md` |
| Monitoring and evaluation (10%) | §10; `docs/monitoring.md` |
| Privacy and responsible AI (5%) | §11; `docs/privacy_responsible_ai.md` |

---

# Part A — The system

## 1. Project title and team

**AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices.** Solo project by Aishah Mohamed Alyammahi.

## 2. Problem definition

**What the problem is.** A cloud platform made of many small services raises thousands of alerts a day, but the engineer on call can read only a few. Real faults are missed because attention runs out, not because monitoring was silent.

**Who experiences it.** On-call engineers and small operations teams (primary), and the users of the platform when a fault is found late.

**Why it matters.** A fault that is noticed late lasts longer. Engineers who are paged for noise stop trusting the pager and start ignoring it ("alert fatigue").

**How it is handled today.** Alerts are escalated by log severity, and exact duplicates are deleted (Alertmanager grouping and routing, PagerDuty rules). Metric alerts are fixed thresholds or rate rules, such as `delta(memory[5m]) > 0.5 GB`.

**Limits of the current approach.** Severity rules read only logs. On the public GAIA microservice benchmark, **82% of injected faults never write a log line**; they show up only as a jump in a metric such as memory, so a log-severity rule catches **0%** of them at 20 items a day (§B1). Fixed metric thresholds either miss faults or flood the queue, and none of these rules knows that a person can read only K items a day.

**How often.** GAIA injects about 22 faults a day on ten services, more than a 20-item budget on 34 of 55 days (§B1).

**If nothing changes.** Teams keep choosing between missed faults and ignored pagers. More monitoring makes it worse, because every new signal competes for the same attention.

**Why this is a software engineering project.** The answer is not one model. It is a system: ingest alerts and metrics from existing tools, decide under a budget in real time, page safely, learn from engineers' feedback, and be deployable, testable, monitored and reversible.

> **Problem statement.** On-call engineers can read only a few alerts a day, but the rules that choose those alerts read log severity, which most real faults never trigger, so faults are missed while noise still reaches the pager.

## 3. Objectives and expected outcomes

**Main objective.** Design, build and evaluate an AI-assisted on-call queue that shows each engineer at most K items a day, in real time, and catches more real faults with fewer false items than the rules teams already run.

**Specific objectives (measurable).**

| # | Objective | Target | Result |
|---|---|---|---|
| O1 | Beat the rules a team already runs at a realistic budget | More faults caught at K = 20 than log severity, a static threshold and a Prometheus rate rule, with fewer false items | Detector 72.6% vs 68.9% (rate rule), 1.0 vs 2.1 false items a day; wins 8/8 walk-forward weeks (§B1, §B2) |
| O2 | Decide in real time | No decision uses later data; confirmation within about a minute | Median 0.72 min; `tests/test_causality.py` (§B1) |
| O3 | Engineer it for production | Authentication, safe paging, versioned models with rollback, monitoring endpoints, Docker | Done (§7, §9, §10) |
| O4 | Test it properly | ≥ 90% of tests passing, coverage ≥ 85%, p95 ≤ 1 s at 50 users | 300/300, 90%, 0.32 s (§8) |
| O5 | Let another team check it on its own data before trusting it | Evaluate my data works end to end without code, against a chance line | Done; NAB, SMD and RCAEval results reported, including failures (§B5) |

**Expected final product.** A working web application and REST API (FastAPI) with a queue page, Connectors page and *Evaluate my data* page; a backend scoring service; an AI component (a cost-sensitive alert classifier with a model registry) next to rule-based metric detectors; a SQLite store; connectors for Alertmanager, Prometheus, PagerDuty, Slack and Opsgenie; a command-line tool; 296 automated tests and CI; a Docker deployment with per-environment settings; `/metrics` and `/monitoring`; technical documentation (`docs/`); and this report.

## 4. Target users and use case

**Primary users.** On-call engineers (site reliability or operations) on a team running microservices.

**Secondary users.** The team lead or operator, who installs the tool, decides when it may page and which model is active; platform owners who read the monitoring page; an evaluating team trying it on past data; examiners and researchers reproducing the results.

| User | Needs |
|---|---|
| On-call engineer | A short list, in order, that contains the real faults; a reason for each item; a way to say "this was noise" |
| Operator / team lead | Safe installation; nothing paged by accident; secrets kept safe; a way to undo a bad model; health at a glance |
| Evaluating team | Proof on its own data, with a fair baseline and a chance line, before trusting it |

**System usage.** Alertmanager posts alerts to the app all day; the app pulls memory metrics from Prometheus; a scoring job runs on each webhook batch (or on demand). The engineer opens the queue page during the shift and receives pages in Live mode. They provide nothing but marks: Real or Noise on items. The system produces the day's queue (metric jumps, slow leaks, monitoring gaps and escalated or "ask a person" alerts, at most K), each with a reason. After that, the engineer investigates the item; the mark becomes a label for the next model.

**Use-case flow.**

```mermaid
flowchart TD
    A[Alertmanager posts firing alerts] --> B[App checks the token and rejects bad rows]
    B --> C[Alerts buffered, duplicates skipped]
    C --> D[Scoring job pulls memory metrics from Prometheus]
    D --> E[Rules: metric jumps, slow leaks, monitoring gaps]
    D --> F[AI: alert classifier scores log alerts]
    E --> G[Causal queue: at most K a day, first come]
    F --> G
    G --> H{Live mode, channel and password?}
    H -- yes --> I[Page PagerDuty / Slack / Opsgenie with new items]
    H -- no --> J[Record what would have been paged]
    I --> K[Engineer reads the queue and marks Real / Noise]
    J --> K
    K --> L[Marks become labels; next model is trained, gated and versioned]
    L --> M[Job, decisions and metrics recorded for monitoring]
```

**UML use-case diagram.** Actors on the left are people; actors on the right are the systems the tool talks to. Scoring a batch always includes training and gating a model when labels exist, and paging (which sends only in Live mode); a Real / Noise mark extends training with a new label. The figure is drawn by `app/make_figures.py`.

![UML use-case diagram of the on-call queue](figures/use_cases.svg)

## 5. Why use AI?

**What needs AI.** Deciding which *log alerts* are worth a slot. Their text, source, severity and timing interact in ways fixed rules do not capture, and the right answer differs per team. A classifier trained on the team's own labels (and later its own Real / Noise marks) learns that, with a miss weighted ten times a false alarm.

**What traditional software handles.** Everything else, deliberately: ingesting webhooks, the metric detectors (a rise above the recent median is a rule, chosen on week 1 only), monitoring-gap detection, the daily budget, paging, storage and security. On GAIA the rules do most of the work: where metric data exists, the jump rule overlaps 99.6% of faults (§B6).

**Why a rule-based approach alone is insufficient for alerts.** Severity is noisy: "high" alerts are often benign and "info" lines appear during real faults. A fixed severity rule cannot use message text or the fact that an alert arrived during a memory jump, and it cannot adapt when engineers mark items as noise.

**AI technology.** Supervised classification: logistic regression on TF-IDF text features, source, severity, recent alert counts and an inside-a-jump flag (scikit-learn). A second learned piece, the confirmation threshold, is a cost-minimising cut on the training window. An experimental jump ranker (logistic regression) was tried and gave no gain (§B4).

**Existing model or API.** An existing, well-understood model family (scikit-learn logistic regression) trained locally; no external AI API or LLM, so no data leaves the server and there is no per-call cost.

**What the classifier adds, measured.** An ablation on GAIA (`run_classifier_ablation.py`, §B4) keeps the whole queue and changes only how log alerts are chosen for their quarter of the slots. At K = 20 the queue with the classifier catches 69.6% with 1.8 false items a day; with log severity instead it catches 71.0% (1.4 false a day); with no log alerts at all, 71.2% (1.2 false a day). **On GAIA the classifier adds no catches; it costs about 1.6 points and 0.6 false items a day**, because 82% of GAIA faults write no log line and the alerts it escalates spend slots that jumps would have used. Its value is therefore not shown on this benchmark. It is kept for teams whose faults *do* log: it is the only part that learns from that team's Real / Noise marks (a Noise mark becomes a negative label at the next retrain), and alerts can never take more than a quarter of the budget. **It is not on by default.** A trained model is used only if, on the held-out part of its batch, it beats the same queue *without* log alerts and the queue it would fall back to by at least one missed fault's worth of cost (§9). The first model has to pass too. On GAIA it does not (848 vs 872 of 1,223 held-out faults caught, §B4), so the tool keeps it as a candidate and scores GAIA with rules only. **On a second dataset it does add catches where faults log.** On RCAEval RE2 Train Ticket the classifier lifts the queue from 60.0% to 73.3% of 45 test faults at K = 10 (+13.3 points, 95% interval +4.4 to +24.4), and the gate promotes it there, by exactly its margin; on Online Boutique, Sock Shop and the RE3 code faults it adds no catch beyond one case, and the gate keeps it out (§B5).

**Limitations of AI here.** It needs labels of both classes, so a new team starts in rules-only mode; learned thresholds do not transfer between datasets (one learned on GAIA's 1 GB faults rejects 0.5 GB faults, §B3); and 82% of GAIA faults leave no log, which caps a log-only model near 18% (§B6).

## 6. AI component and technical approach

| Question | Answer |
|---|---|
| AI technology | Cost-sensitive logistic regression for log alerts; rule-based metric detectors beside it |
| Existing model or own? | An existing model family, trained on the team's own labelled data; not a new algorithm |
| Input | Each alert: time, source, severity, type, message; whether it fell inside a confirmed memory jump; recent counts from the same source |
| Output | P(alert is a real fault), turned into an action: escalate, ask a person, or hide, with a reason |
| Data needed | Past alerts with incident windows, or Real / Noise marks; memory series for the detectors |
| How the software talks to it | In-process: `pipeline.score_alerts` calls the classifier; models are loaded from the registry (`product/model_store.py`) |
| If it is wrong | The queue is capped at K; uncertain alerts go to a person; engineers mark Noise; a model is not promoted unless it beats the queue without log alerts and the queue it replaces by at least one missed fault's worth (and is no worse than the active model) on held-out data, and the operator can roll back |
| Main limitation | Label-hungry; helps only services that log |

**How the queue decides** (details in §B1 and `design_specification.md`). One queue, at most K items a day, first come, first served:

1. **Metric jumps.** A point is hot when it is more than **0.5 GB** above the median of the previous **15 minutes** on the same service. Two hot points no more than two minutes apart **confirm** a jump. A jump is shown at its confirmation point, and only if its rise at that point is at least a threshold learned on the training window (0.62 GB on week 1).
2. **Slow leaks.** The same rule with a **3-hour** median and 10 minutes of hot points catches memory that climbs over hours. A leak is dropped only when a fast jump on the same series was already shown when the leak was confirmed.
3. **Monitoring gaps.** A service with no metric point for **10 minutes** is shown as a gap item ("we are blind here"), also while the feed is still dead.
4. **Log alerts** may use at most **a quarter** of the slots (5 of 20), first come. An alert on a service whose shown jump is still open rides on that jump and uses no slot.

Nothing uses a later point, the end of a jump, or its peak (ADR 0001). For metrics that are not bytes, a relative mode (a share of the median) and a unit-free robust mode exist.

**Training on GAIA.** GAIA MicroSS (CloudWise), July–August 2021: 34,863 log lines, 1,367 injected faults, each holding **1 GB of extra memory for 600 s** on one service. Train on the first 7 days; test on the next 55 days (**1,223 faults**); days are never shuffled.

| What is learned | How |
|---|---|
| Jump size 0.5 GB | Chosen on week 1 only (`run_memory.py --sweep`) |
| Jump confirmation threshold | Rise at confirmation with the lowest week-1 cost → **0.62 GB** |
| Alert classifier and its threshold | Logistic regression on week-1 alerts; threshold with the lowest week-1 cost (a miss = 10 false alarms) |
| Static-rule baseline | Week-1 99th percentile per service |

**Production design.** Two layers: a library that knows nothing about HTTP (`app/src/`) and a product layer (`app/product/`) with the web app (`product/web/`: security middleware, three route modules), the job pipeline (inputs, labels, paging), storage, connectors, the model registry and monitoring. Diagrams (context, components, ingest-to-page sequence, deployment, data model): `docs/architecture.md`. Decisions: `docs/adr/`. Threats and mitigations: `docs/threat_model.md`.

## 7. Deployment strategy

One Docker container per team, on the team's own server or VM, behind a TLS reverse proxy; SQLite and the model registry on a Docker volume; the model runs in-process; alerts arrive from Alertmanager and metrics are pulled from Prometheus; pages go to PagerDuty, Slack or Opsgenie. The image is built from a hash-pinned lock file.

| | Development | Test | Production |
|---|---|---|---|
| Where | Laptop | GitHub Actions; a shadow instance next to production | Team server |
| Settings | `app/deploy/dev.env` | `app/deploy/test.env` | `app/deploy/prod.env` (from the example; not in git) |
| Paging | Never | Never (records *would have paged*) | Only after a shadow period and a deliberate Live switch |

Measured capacity: 50 concurrent users at 228 requests/s with p95 0.32 s; a 400-alert webhook batch scores in 0.2 s and the full GAIA month in about a minute (54.9 s, below; target ≤ 5 min). The design runs one worker per team; the scaling path is one container per team, or PostgreSQL with shared locks for a shared deployment (ADR 0003). Sign-in sessions, the login lock and the ingest rate counters are kept in `auth.db` (SQLite beside `ops.json`; session tokens stored as SHA-256), not in process memory, so a restart or redeploy no longer signs everyone out or clears a lock earned by guessing passwords (version 1.0.2; `test_auth_store.py` starts the app twice in separate processes). The job, evaluation and `ops.json` locks are still in-process, so the app stays one worker.

**Resources and operating cost (learning objective 11).** Measured with `app/perf/resources.py`, which runs the product's own scoring job on the GAIA month (34,863 alerts, 62 days, shadow mode) against a throw-away database on the author's Windows laptop (20 logical CPUs, Python 3.12; `results/resources_gaia.json`):

| Resource | Measured |
|---|---|
| Wall time for the whole month in one job (load, score, train and gate, store) | 54.9 s (median of 3 runs: 53.7, 54.9, 55.7 s) |
| Peak memory of the process | 505 MB (173 MB of it is Python and the libraries before the job) |
| Database growth | 13.0 MB for 62 days: about 215 KB per day at GAIA's alert volume |
| One saved model version | 0.02 MB |
| Calls to an external AI service | none, so $0 per call |

**One timing, one machine.** Earlier versions of this report gave two times for the GAIA month: 27 s (a manual timing from 28 September, with no record of the machine or code version) and 35.7 s (one run of `perf/resources.py` on 11 October). Both were re-measured on 11 October on the same laptop with the same code, three runs of `perf/resources.py`: 53.7, 54.9 and 55.7 s. The laptop was on battery power (Windows *Balanced* plan) with other programs open, which may explain why it was slower than the earlier run; the 27 s figure cannot be reproduced and is no longer used. Every document now gives this one measurement. Wall time depends on the machine; the target (≤ 5 min) holds either way.

The cost estimate below rests on **assumptions**, stated so they can be changed: one container per team, running all month (730 hours), on a 2 vCPU / 4 GiB cloud VM (the measured peak of 505 MB leaves room for the web app and the operating system), with a 20 GB disk. Prices are AWS list prices for US East (N. Virginia), Linux, read on 11 October 2026: t3.medium $0.0418 an hour on demand and $0.025 an hour with a 1-year reservation ([AWS T3 page](https://aws.amazon.com/ec2/instance-types/t3/)); gp3 disk at $0.08 per GB-month (the rate in [AWS's EBS pricing examples](https://aws.amazon.com/ebs/pricing/)). Other providers and regions differ; a team's own server costs only its share of an existing machine.

| Item | Per team per month |
|---|---|
| VM, on demand (0.0418 × 730) | $30.51 |
| VM, 1-year reservation (0.025 × 730) | $18.25 |
| Disk, 20 GB gp3 (one year of jobs at GAIA's volume is about 78 MB) | $1.60 |
| AI inference | $0 (in-process scikit-learn model) |
| **Total** | **about $20–32** |

Not priced: data transfer (pages are a few KB), backups (a copy of a 13 MB database), and the largest cost, people: the queue exists to spend on-call hours on at most K items a day.

**AI governance.** The operator is responsible for the AI. It may only order and select items in the queue (escalate, ask a person, hide with a reason); it never acts on infrastructure. Humans review everything in shadow mode, answer "ask a person" items, and mark Real / Noise. Every decision records its reason, score and the model version. New models pass a promotion gate and can be rolled back; the change is audited. No data is sent to an external AI service. Full answers: `docs/deployment.md`.

## 8. Testing strategy

| Level | What | Examples |
|---|---|---|
| Unit | Detectors, queue, policy, scoring, settings | first come beats biggest first; a jump is confirmed at its second point |
| Integration | Frontend routes ↔ backend ↔ database, with outbound calls mocked | webhook → buffer → job → queue; Live fans out to every channel; model rollback through the API |
| System | The whole pipeline on GAIA, NAB, SMD and RCAEval; `experiment.py` on a synthetic run | walk-forward over 8 weeks; later data changes nothing |
| AI-specific | Wrong or poor outputs, unexpected input, edge cases | a malformed webhook row is rejected instead of blocking every later job; one-class labels fall back to rules; a worse model is not activated; hidden alerts stay visible |
| Security | Login, SSRF, CSRF, DNS rebinding, secrets, body limits | loopback tricks refused; forwarded requests need a password |
| Performance | Latency, concurrent users, throughput | `perf/load_test.py`: 20 and 50 users |

Hallucination and prompt injection do not apply (no generative model), but the equivalent risks are tested: untrusted webhook content cannot break or steer the queue, and wrong model outputs are bounded by the budget and the promotion gate.

| Criterion | Target | Result |
|---|---|---|
| Tests passing | 100% | 300 / 300 (299 on Python 3.10 and 3.12, 1 in the integration job) |
| Line coverage | ≥ 85% | 90% |
| Type errors | 0 | 0 |
| p95 latency at 50 users | ≤ 1 s | 0.32 s |
| Error rate under load | ≤ 1% | 0% |
| Report numbers match results | every checked number | 193 / 193 |

CI runs lint, type checks, the tests on two Python versions with coverage, `run_validate.py`, a dependency audit (`pip-audit` on the hash-pinned lock) and a Docker build with a health check on every push. The audit step was added on 7 October with an option `pip-audit` does not have, so that job failed on that commit; it was corrected on 10 October (a hand run of the corrected command found no known vulnerabilities). Details: `docs/test_plan.md`.

**A bug only a real-service test found (version 1.0.2).** Live Prometheus data could never confirm a jump. The ingest job fetched 14 days of memory in one query; to stay under Prometheus' point limit, that query used a 121 s step, and the jump rule links hot points at most 2 minutes apart, so no live jump was ever confirmed. The unit tests fed the detector data directly and missed it. A new integration test (`app/integration/docker-compose.yml`, `tests/integration/test_compose.py`) runs the app beside a real Prometheus and Alertmanager and checks webhook → ingest job with Prometheus series → nothing paged in shadow mode; it found the bug. The fetch now reads the window at 60 s in several non-overlapping queries (`test_a_14_day_fetch_keeps_one_minute_points_so_live_jumps_are_confirmed`), and CI runs the integration test in its own job. The GAIA, NAB, SMD and RCAEval results do not use the Prometheus fetch, so no published number changed.

## 9. MLOps

- **Versioning.** Code in Git through pull requests; dependencies in `uv.lock` and a hash-pinned `requirements.lock`; configuration in `src/config.py` and per-environment files; data by fingerprint in the experiment log; **models as numbered versions with model cards** in `models/registry.json` (training time, data fingerprint, code version, held-out results, gate decision). No prompts (no LLM).
- **Reproducibility.** `uv sync --group dev`, `uv run pytest`, `uv run python run_validate.py`, `docker compose up`; every environment variable is documented; the public datasets are fetched by scripts (`tools/fetch_gaia.py`, `fetch_nab.py`, `fetch_smd.py`, `fetch_rcaeval_mem.py`, `fetch_rcaeval_re23.py`), and `fetch_gaia.py` and `fetch_rcaeval_re23.py` check the SHA-256 of every file used.
- **Model updates.** A new model is trained when a batch has labels of both classes, on the batch's first 7 days, and tested on the rest. It becomes active, and scores the batch, only if on that held-out window (1) it costs less than showing no log alerts (a miss = 10, a false alarm = 1); (2) the whole queue with its alerts costs less (a missed fault = 10, a false item = 1) than both the queue without log alerts and the queue the batch falls back to if the model is refused (the active model, or rules only = severity), by at least a margin of one missed fault (10; `PROMOTION_MIN_SAVING_MISSES`, default 1), so the model is tested against what would actually run and a saving that one held-out case could produce on its own does not promote it; and (3) it costs no more than the active model on the same held-out alerts. The first model is not promoted just because none is active. A model that fails stays a candidate, and the batch is scored by the active model, or by rules only. On GAIA the classifier passes (1) and fails (2) (§B4), so rules only stays. The operator can roll back to any version (`POST /models/{version}/activate`, hash-checked, audited). Precision from engineers' marks and alert-volume drift are monitored, and a retrain is suggested after 7 days or new Real marks.
- **Why the margin is one missed fault.** The held-out queue cost is counted in whole faults (10 each) and false items (1 each), so a single held-out case that flips moves it by 10. A saving smaller than that could come from one case alone, and the held-out windows are small (14–45 faults on RCAEval). Before the margin, the classifier was promoted on RCAEval RE2 Online Boutique for 3 false items fewer in 45 days (cost 283 against 286 by severity and 291 without log alerts); it is now kept as a candidate. The margin is a setting, not a fit: 0 restores the earlier rule (any strict saving), and a team that wants more evidence can raise it.
- **Retrain and rollback triggers.** Numeric, from the signals the tool already records. *Automatic* rows are enforced by code; *operator* rows are the runbook (the tool shows the signal, a person acts; it never rolls back on its own).

| Trigger | Threshold | Action | Who |
|---|---|---|---|
| Model age | ≥ 7 days since training (`RETRAIN_AFTER_DAYS`) | `retrain_due` reminder on the Queue page: run a job with labels (every such job trains and gates a new model) | Automatic flag |
| New labels | ≥ 1 new Real mark since training | Same reminder | Automatic flag |
| Alert-volume drift | last batch ÷ median of earlier batches outside 0.5–2 | Retrain on the next batch; check the alert sources | Operator, from `/monitoring` |
| Precision from marks | < 0.70 with ≥ 20 marks | Retrain; if it stays < 0.70 for the next 20 marks, roll back | Operator, from `/monitoring` |
| Promotion gate | new model's held-out queue does not cost at least one missed fault (10) less than both the fallback queue and the no-log-alerts queue | Model is not activated (kept as a candidate) | Automatic |
| After an activation | precision from the first 20 marks < 0.70, or more than 0.10 below the previous version's | Roll back: `POST /models/{version}/activate` with the previous version | Operator |
| After an activation | escalated alerts per job more than twice the median of the 7 jobs before it, or a failed scoring job | Roll back as above, then investigate | Operator |

- **Testing before release.** CI for code; the promotion gate and shadow mode for models.
- **Experiment tracking.** Every experiment script appends to `app/results/experiments.jsonl`: commit, library versions, configuration, data fingerprint, output hashes, headline metrics and the reason for the run.

Details: `docs/mlops.md`.

## 10. Monitoring and evaluation

`GET /metrics` (Prometheus) and `GET /monitoring` (checked summary) cover:

| Area | Signals | Target |
|---|---|---|
| AI performance | Precision from Real / Noise marks; model used and version per job; promotion decisions | ≥ 0.70 with ≥ 20 marks |
| Software / system | Latency by route, 5xx rate, job duration and outcome, pages sent and failed, `/health` | p95 ≤ 1 s; 5xx ≤ 2%; 0 failed pages |
| Data | Alert-volume drift (last batch vs median), monitoring gaps, rejected and duplicate rows, buffer size | ratio 0.5–2; ≤ 3 gaps |
| AI / model | Output mix per job (escalated / asked / hidden), items by kind | Watch for sudden shifts |

Evaluation criteria with measured values, justification for each target and example alert rules: `docs/monitoring.md`. Offline evaluation is Part B.

## 11. Privacy and responsible AI

**Privacy.** Alert messages may contain host names or user IDs. All data stays on the team's server (SQLite and a volume), behind the login. Messages are cut to 300 characters, and IDs and numbers are normalised before learning. Jobs and evaluations are pruned; marks keep only service and time (retention table below). No data is sent to an external AI service; only paging summaries go to the team's own paging tools, in Live mode.

**Data retention.** Most limits are counts, set by environment variables; the period they give depends on how often jobs run. The periods below assume one scoring job an hour (Alertmanager batches) where a count is converted; rows marked *policy* are what the operator should enforce, because the code does not delete them.

| Data | Kept by the code | Period (assumption: one job an hour) | Policy |
|---|---|---|---|
| Ingest buffer (raw webhook rows) | until the next successful job | ≤ 1 hour | — |
| Duplicate fingerprints | `INGEST_DEDUP_HOURS` = 24 | 24 hours | — |
| Jobs: alerts, decisions, items | newest `JOB_RETENTION` = 200 jobs | about 8 days (200 days at one job a day) | — |
| *Evaluate my data* uploads | newest `EVAL_RETENTION` = 30 | depends on use | delete uploads after 30 days |
| Real / Noise marks (kind, service, time window; no message) | kept | — | 12 months, the training window a team needs |
| Model versions and model cards | every version that was ever active is kept; refused candidates keep their file only for the newest `MODEL_CANDIDATE_KEEP` = 20; every card is kept | size-bound for candidates | keep the active version and the last 5; delete older after 12 months |
| Audit log file | rotated at `AUDIT_LOG_MAX_BYTES` = 1 MB, one old file kept | size-bound | ship to the team's log store and keep 12 months |
| Backups (`product.backup`) | in a folder the operator owns | — | 30 days |

**Finding and deleting one person's data (a data-subject request).** People named in logs have no table of their own: their ID (user ID, e-mail, host name) can only appear in text the team sent. Everything is on the app's volume (`/app/product/data`). Stop the app (`docker compose stop`), open a shell on the same volume (`docker compose run --rm oncall-queue sh`, then `cd product/data`), and for an ID `X`:

1. **`queue.db`** (`python -m sqlite3 queue.db`). Find it with `instr(column, 'X')` in `decisions.message`, `decisions.alert_id` and `decisions.source`; `jobs.meta_json` (the *would have paged* summaries carry up to 200 characters of a message); and `ingest_buffer.payload_json` (raw webhook rows not yet scored). Erase with `UPDATE decisions SET message = replace(message, 'X', '[erased]') WHERE instr(message, 'X');`, the same `replace` on `jobs.meta_json`, and `DELETE FROM ingest_buffer WHERE instr(payload_json, 'X');`. Redacting keeps the queue history and the item ids that Real / Noise marks point to. `jumps` and `feedback` hold no message text (kind, service and times only); `ingest_seen` holds SHA-256 fingerprints that expire after 24 hours.
2. **`auth.db`** (operators, not people in the logs): `sessions.user` is the login name (`DELETE FROM sessions WHERE user = 'X';` signs them out); `events.client` and `locks.client` are client IP addresses.
3. **Files.** `grep -rl 'X' audit.log* evals/` finds audit lines (`user=`, `client=`) and *Evaluate my data* uploads (`evals/<id>/*.csv`): delete that evaluation folder and edit the audit file. `model.pkl` and `models/*.pkl` hold a text vocabulary of at most 300 terms, learned after IDs and numbers are normalised, so an ID is normally not in it; if a name is, train a new version on the cleaned data, activate it and delete the old file.
4. **Copies.** Backups from `product.backup` hold the same files (apply the same steps, or let them expire after 30 days); summaries already paged to PagerDuty, Slack or Opsgenie must be deleted there.

**Responsible AI.** Fairness between services: metric rules treat every service the same, and alerts are capped at a quarter of the budget, so services that log a lot cannot crowd out the rest. Every item carries a reason. Humans oversee through shadow mode, "ask a person" and marks. Hidden alerts stay visible. The system never acts on infrastructure. Queue data should not be used to rate engineers. Details: `docs/privacy_responsible_ai.md`.

## 12. Project scope and limitations

**In scope (built):** the causal budgeted queue (jumps, leaks, gaps, alerts); the alert classifier with a model registry, promotion gate and rollback; the web app, API and CLI; Alertmanager, Prometheus, PagerDuty, Slack and Opsgenie connectors; *Evaluate my data*; shadow mode and marks; security hardening; monitoring endpoints; Docker and per-environment settings; CI; the GAIA, NAB, SMD and RCAEval evaluations.

**Out of scope (not built):** automatic remediation; multi-tenant hosting and user roles; LLM summaries of incidents; dedicated CPU, disk or network fault detectors inside microservices (the unit-free jump rule is measured on such faults on SMD servers and RCAEval RE2, §B5); a managed cloud deployment; browser UI tests; a live pilot with a real team (the next step).

**Limitations** (say these if asked):

1. **Every GAIA fault is the same 1 GB, 600 s memory program.** The robustness replay (§B3) tests other sizes and shapes. §B5 tests other domains (NAB, a shop, SMD servers) and, on three other microservice applications, the same memory injection and CPU, disk, network and code faults with logs (RCAEval), with small test sets and a chance line.
2. **The win over a plain Prometheus rate rule is modest** (+3.7 points for the detector, 8/8 weeks; +0.7 for the full queue, which ties it week by week). The larger gains are fewer false items, a real-time queue, and leak and gap coverage. Carry-over lifts both rules about equally.
3. **K = 20 is below the fault rate** (22 a day), so no method can exceed 84.9%.
4. **Learned thresholds do not transfer.** One learned on GAIA's 1 GB faults rejects 0.5 GB faults. Rules-only mode works without labels; the per-series confirm levels (§B4) are measured but not in the headline.
5. **The AI component adds no catches on GAIA.** With log alerts chosen by severity, or left out, the queue catches slightly more (71.0% and 71.2% vs 69.6% at K = 20) with fewer false items (§5, §B4). The classifier is kept for teams whose faults log and who mark items: on RCAEval RE2 Train Ticket, where faults log on the broken service, it adds 13.3 points over no log alerts (95% interval +4.4 to +24.4), and the gate promotes it, by exactly its one-fault margin over severity alerts; on the other five RCAEval application suites it adds at most one case, and the gate keeps it out (§B5). The promotion gate now checks this on every batch, and on GAIA it keeps the classifier out (§9, §B4).
6. The scripts in `app/experiments/legacy_alerts_only/` are alerts-only milestone experiments. Do not quote them as the system result.

**Future work.** A live, continuous log stream (RCAEval recordings are 24–30 minutes, replayed one a day) to confirm the RE2 result for the classifier; a pilot with one team in shadow mode, measuring precision from marks. R1 through R8 in `docs/threat_model.md` are closed and tested: the Prometheus fetch uses the address it checked, login locks out (behind a trusted proxy the client is read from the right of `X-Forwarded-For`, across every header line; a code review on 10 October found the leftmost, client-written value was used, which let a client dodge the lock, and that only the first header line was read; both are fixed with regression tests), paging is by item key, models load through skops, sign-in sets a session cookie, a viewer password cannot change settings, the audit log is also a file, and paging secrets stay out of `ops.json`.

## 13. Success criteria

| Criterion | Measured | Met? |
|---|---|---|
| The main workflow (webhook → queue → page → mark → retrain) completes | Integration tests and the demo | Yes |
| At least 90% of predefined test cases pass | 300 / 300 | Yes |
| The system handles the agreed concurrent users | 50 users, 0 errors | Yes |
| Average response time below the threshold | p50 0.21 s, p95 0.32 s (target 1 s) | Yes |
| AI output meets the predefined quality criteria | Detector beats the rate rule in 8/8 weeks with fewer false items; the full queue ties it; the alert classifier adds no catches on GAIA (§B4 ablation), and the promotion gate keeps it out there; on RCAEval RE2 Train Ticket it adds 13.3 points with an interval above zero, and the gate promotes it, at its margin (§B5) | Partly: the detector, yes; the full queue, a tie; the classifier, on one of six RCAEval application suites, not on GAIA |
| No critical security vulnerabilities in testing | 0 critical. R1 through R8 are closed and tested | Yes |
| Deployable from documented instructions | `docker compose up`, `docs/deployment.md`, CI Docker job | Yes |
| Another person can reproduce the environment | `uv sync`, lock files, `run_validate.py` | Yes |
| The agreed functional requirements are met | 20 FR, each traced to tests (`docs/requirements.md`) | Yes |

---

# Part B — Results

## B1. Test-period results (K items a day, 55 days, 1,223 faults)

**Caught** = a shown jump or leak on the fault's service overlaps the injection window, or a shown alert carries the fault's id. Gap items never count as catches.

**Baselines are the rules a team would already have**, all scored with the same causal queue:

| System (K = 20 items a day) | Caught | Share of best possible | Items / day | False / day | Precision |
|---|---|---|---|---|---|
| Log severity + exact duplicates | 0.0% | 0% | 0.8 | 0.8 | 0% |
| Static rule: memory > week-1 p99 | 57.2% | 67% | 16.0 | 3.0 | 81% |
| Rate rule: `delta(mem[5m]) > 0.5 GB` | 68.9% | 81% | 17.4 | 2.1 | 88% |
| Jump rule, first come | 67.8% | 80% | 17.5 | 2.4 | 86% |
| **Jump rule + learned threshold** (detector alone) | **72.6%** | **86%** | 17.1 | **1.0** | **94%** |
| **Full queue with the classifier** (jumps + leaks + gaps + classifier alerts; refused by the gate on GAIA) | **69.6%** | **82%** | 17.7 | 1.8 | 90% |
| **Shipped configuration** (the gate keeps the classifier out: jumps + leaks + gaps + log alerts by severity) | **71.0%** | **84%** | 17.6 | 1.4 | 92% |
| *Old headline: day's jumps ranked by final peak (not real time)* | *72.9%* | *86%* | *17.5* | *1.3* | *93%* |

**What the tool runs on GAIA is the shipped row.** The promotion gate refuses the classifier on GAIA (§B4), so the tool scores log alerts by severity; that row is the same queue with severity alerts (`run_classifier_ablation.py`, `severity`). The full-queue row is kept because every later comparison (walk-forward, options, ablation) was measured on it.

**Best possible at K = 20 is 84.9%**: GAIA injects 22 faults a day, more than 20 on 34 of 55 days.

At K = 5 / 10 / 50 the full queue catches 19.5% / 38.8% / 85.8% (rate rule: 19.3% / 38.6% / 86.0%). Its advantage is at the realistic budget K = 20, where slots are scarce, and in false items (1.0–1.8 a day vs 2.1).

**Removing the last look-ahead.** A code review found five places where the queue still used information from later in the day, although the text said it did not. The per-day alert filters kept each day's *most severe* (or highest-scoring) K alerts, which needs the whole day; an overlapping jump that arrived *after* a leak was confirmed could still remove that leak; the robust score used the spread of the whole series; and an alert "rode on" a jump using the jump's final end. All five now use only what is known at the moment of the decision: alerts spend the budget first come, first served; a leak is dropped only by a jump already shown; the robust spread uses earlier points only; and an alert rides on a jump only while the jump's last hot point is within its gap (`Spike.open_at`). `tests/test_causality.py` checks each one. The honest cost is small: the full queue drops from 70.2% to 69.6% at K = 20 and shows 1.8 false items a day instead of 1.6. The detector alone is unchanged (72.6%), because none of the five touched it.

**Why the full queue scores below the detector alone on GAIA.** Every GAIA fault is a 1 GB memory fault, so leak items (0.2 a day), gap items (0.4 a day) and log alerts spend slots that jumps would have used. On a platform with leaks, outages, and CPU, disk or code faults, those are the parts that catch them (§B3). Both rows are reported; the detector comparison is the first claim. The full queue is the published configuration with the classifier on; the tool's promotion gate turns the classifier off on GAIA (§B4).

The old ranking needed the whole day in advance. Replacing it with a learned confirmation threshold keeps the score (72.6% vs 72.9%), cuts false items (1.0 vs 1.3 a day), and makes the queue usable live.

**Time to confirm.** Median **0.72 minutes** from fault start to the confirming point (p90 0.93). Metrics are sampled every 30 seconds.

**Chance check.** If each service's jumps are shifted by a random 1–6 hours, only **2.6%** of faults still overlap one.

![Faults caught versus daily budget K](figures/detection_vs_k.svg)

### Where the misses come from

The full queue misses 372 of the 1,223 test faults at K = 20 (`results/miss_causes_gaia.csv`):

| Cause | Faults | Share of misses |
|---|---|---|
| No metric data during the fault (monitoring outage) | 166 | 45% |
| A qualifying jump existed, but the day's 20 slots were used | 198 | 53% |
| Metric data, but no jump overlapped | 4 | 1% |
| A jump overlapped, but below the confirmation threshold | 4 | 1% |

**Only 2% of misses are the detector's fault.** The rest are data outages (which the queue flags as gap items) and a budget smaller than the fault rate.

---

## B2. Validation (walk-forward)

Before each later week, **everything learned is re-learned from the past only** — the classifier, its threshold, the jump ranker and the jump confirmation threshold (0.62–0.75 GB across folds). That week is then scored once. `run_walkforward.py`, K = 20.

| Fold | Test week | Faults | With metric data | Rate rule | Detector alone | Full queue |
|---|---|---|---|---|---|---|
| 1 | 8–15 Jul | 161 | 100% | 77.6% | 84.5% | 82.0% |
| 2 | 15–22 Jul | 134 | 91% | 73.9% | 81.3% | 74.6% |
| 3 | 22–29 Jul | 162 | 94% | 72.2% | 77.2% | 71.0% |
| 4 | 29 Jul–5 Aug | 163 | 82% | 67.5% | 69.3% | 67.5% |
| 5 | 5–12 Aug | 162 | 84% | 69.1% | 71.6% | 71.0% |
| 6 | 12–19 Aug | 139 | 92% | 75.5% | 79.1% | 74.1% |
| 7 | 19–26 Aug | 173 | **61%** | 51.4% | 52.0% | 50.9% |
| 8 | 26–31 Aug | 129 | 91% | 78.3% | 83.7% | 79.1% |
| **Mean** | | | | **70.7%** | **74.8%** | **71.3%** (95% CI 64.5–76.7%) |

- The **detector** (jump rule + learned threshold) beats the rate rule in **8/8** weeks (sign test p = 0.0039).
- The **full queue** does **not** beat the rate rule week by week: 4 wins, 3 losses, 1 tie (p = 0.5). Before the look-ahead fixes in §B1 it won 6 of 8; part of that margin came from choosing each day's alerts with hindsight. On GAIA its extra item types cost slots; see §B1.
- The comparison is against a real rule. The log-severity rule scores 0% every week, so a test against it would prove nothing.

**Fold 7** is weak because of a **monitoring outage**: every service's metrics stop from 20 Aug 13:18 UTC for 2 days 12 hours, so only 61% of that week's faults have any metric data. The full queue shows this outage as a gap item within 10 minutes. No metric-based method could catch those faults.

![Walk-forward detection by later week](figures/walkforward.svg)

---

## B3. Robustness: does it only work because every fault is 1 GB?

`run_robustness.py` adds synthetic faults of other shapes to the **real** GAIA memory series (120 per shape, at least an hour from any real fault) and asks whether each detector fires, with no budget:

| Injected shape | Jump rule 0.5 GB | + learned threshold | Relative +30% | Rate rule | Jump + leak rule |
|---|---|---|---|---|---|
| Step +0.25 GB, 10 min | 1% | 0% | 8% | 1% | 1% |
| Step +0.5 GB, 10 min | 99% | **0%** | 58% | 99% | 99% |
| Step +1 GB, 10 min (GAIA shape) | 100% | 98% | 95% | 100% | 100% |
| Step +2 GB, 10 min | 100% | 100% | 100% | 100% | 100% |
| Spike +1 GB, 1 min | 98% | 98% | 92% | 100% | 98% |
| Ramp +1 GB over 30 min | 1% | 1% | 6% | 0% | **99%** |
| Leak +1 GB over 3 h | 12% | 3% | 6% | 8% | **73%** |
| Nothing injected (control) | 6% | 3% | 6% | 6% | 6% |

What this shows:

- **Faults smaller than the jump size are invisible** (0.25 GB). A team must set the size for its own services.
- **The learned threshold over-fits the benchmark**: trained on 1 GB faults, it rejects every 0.5 GB fault. The tool therefore uses **no** confirmation threshold unless it is trained on the team's own incidents.
- **Leaks and ramps defeat both the jump rule and the Prometheus rate rule.** The leak rule catches 99% of 30-minute ramps and 73% of 3-hour leaks, at a cost of 0.3 extra items a day on real GAIA data (none of them near a fault), and no extra hits on the control windows.

---

## B4. Queue options

| Option (K = 20) | Caught | Items / day | Walk-forward mean | Verdict |
|---|---|---|---|---|
| Full queue (reference) | 69.6% | 17.7 | 71.3% | — |
| **Learned jump ranker** instead of the rise threshold | 69.7% | 17.7 | 71.8% | No gain: the rise threshold already gives 94% precision, so there is little left to learn from service, time of day or nearby logs |
| **Group** metric items within 2 min on other services into one slot | 70.7% | 17.6 | 72.6% | Small gain; one item may then name two services |
| **Confirm level per series** (learned on week 1; off in the headline) | 69.3% | 17.6 | 70.5% | Mixed: the detector alone keeps 72.6% with fewer false items (0.8 vs 1.0 a day), but the levels reject real faults — 26 misses below threshold instead of 4, and 84.1% vs 85.8% at K = 50. The web app uses it; the published tables use the single gate |
| **Carry over** up to 10 unused slots to the next day | **72.6%** | 18.6 | **74.8%** | Largest gain; the average stays under K. But the same carry-over lifts the rate rule to 72.5% (walk-forward 74.3%), and the full queue then wins only 3 of 6 decided weeks (p = 0.66). Before the look-ahead fixes this row read 74.4% and 8/8 weeks; that margin did not survive |

**Ablation: what the alert classifier adds** (`run_classifier_ablation.py`, `results/classifier_ablation_gaia.csv`). Same full queue, same week-1 training; only the choice of log alerts for their quarter of the slots changes. The classifier row reproduces the published full-queue numbers exactly.

| Log alerts in the queue (K = 20) | Caught | Items / day | False / day | Precision |
|---|---|---|---|---|
| Chosen by the classifier (the full queue) | 69.6% | 17.7 | 1.8 | 90% |
| Chosen by log severity + exact duplicates | 71.0% | 17.6 | 1.4 | 92% |
| None (metric items only) | 71.2% | 17.4 | 1.2 | 93% |

On GAIA the classifier does not earn its slots: on net, the alerts it escalates catch fewer faults than the metric items they push out of a full day. At K = 50, where slots are not scarce, all three catch 85.8%. This is why the detector alone is the first claim (§B1) and why the classifier is described as an option for teams whose faults log (§5).

Carry-over and grouping are off by default (`CARRY_OVER_MAX`, `GROUP_MINUTES`): they change what "20 a day" means for the on-call team, so it is their choice.

---
**Why the severity row attaches no alerts.** An alert rides on a jump (no slot of its own) only if it arrives while a shown metric item on *its own service* is still open (`build_queue`). The classifier row attaches 280 alerts that way. The severity row attaches none, and the data explains why (`results/classifier_ablation_gaia.json`). The 507 test alerts that carry a fault are all `warning` (471) or `info` (36); no test alert is `critical` or `high`. So the severity rule never escalates a fault's alert. It escalates 44 `error` lines, with 14 more cut by its own 20-a-day limit. All 44 fall on 4 days, none carries a fault, and they are nightly jobs reporting that a log upload failed. None arrives while a shown metric item on its service is open: the nearest one on the same service is at least 199 minutes away (median 617). So 11 of them take a slot of their own, the rest find the day's alert share or budget used, and the row ends up close to "no log alerts".

**What the shipped gate does with this.** The tool trains the classifier on the first 7 days of a batch and tests it on the rest (§9). A model must cost less than showing no log alerts on the held-out alerts, and the whole held-out queue with its alerts must cost less than both the same queue without log alerts and the queue that runs if the model is refused (here rules only, log alerts by severity), by at least one missed fault (10). The first model is not promoted just because no model is active. On GAIA the classifier passes the first test: it finds 339 of the 507 fault-bearing held-out alerts. It fails the second: with its alerts the queue catches 848 of 1,223 held-out faults with 95 false items, against 871 with 70 false items with severity alerts and 872 with 58 false items without log alerts. So it is registered as a candidate, and GAIA is scored by rules only (log alerts by severity, as in the second row). The gate's queue is the tool's, not this table's (it uses the tool's own confirm settings), so its counts differ a little from the rows above; the direction is the same. `run_classifier_ablation.py` records the decision in the same JSON file.


## B5. Beyond microservices: any metric, any team

The engine is not tied to GAIA. `src/generic.py` takes any **metrics** table (time, value, optional series; long or wide; column names detected), optional **events** (alerts, tickets, notifications) and optional **past problems** (start, end, series). For each series it tunes on the first part and scores the rest once, and every system — static threshold, rate rule, a unit-free robust jump rule, the user's own alerts — is tuned with the same cost and scored through the same causal queue. The detector with the lowest training cost becomes the user's queue. A **chance line** (the same items at random times) is reported so wide labels cannot flatter the result.

This runs from the web app (**Evaluate my data**: upload or pick a dataset → read the report → *Use these settings for my live queue* → *Show this data in the queue*) and from the CLI (`evaluate`, `tune`). The guide for end users is `USE_YOUR_DATA.md`.

Results on data that is not microservices (tuned on the first half of each series, K = 2 items a day; `run_nab.py`, `results/nab_eval.csv`):

| Dataset | Domain | Test problems | Queue caught | Chance | Items / day |
|---|---|---|---|---|---|
| Online shop (synthetic; K = 5, 30% tuning) | e-commerce orders + notifications | 11 | 91% | 1% | 1.3 |
| NAB realAWSCloudwatch | cloud servers | 17 | 71% | 58% | 0.3 |
| NAB realAdExchange | online advertising | 6 | 83% | 58% | 1.4 |
| NAB realKnownCause | machine temperature, taxi demand, CPU | 13 | 38% (static rule 54%) | 15% | 0.04 |
| NAB realTraffic | road traffic sensors | 11 | 36% (rate rule 45%) | 12% | 0.3 |
| NAB realTweets | Twitter mention volume | 11 | 36% | 36% | 1.7 |

On the shop data the monitoring view catches 10 of 11 problems where the notification stream by severity catches 3. On NAB the queue is clearly above chance on traffic and machine data, modestly on cloud servers and ads (NAB's anomaly windows are wide, so chance is high), and **no better than chance on Twitter volume**. The test sets are small (6–17 problems), and on one dataset the detector chosen on the tuning part was not the best on the test part. These are exactly the facts a new user needs before relying on it, which is why the app shows them.

**Other fault types: the Server Machine Dataset (SMD).** GAIA has one fault type. SMD (OmniAnomaly, KDD 2019) has 28 real servers, five weeks of one-minute samples, 38 metrics each — CPU, memory, disk, network, TCP — and 196 labelled test incidents, each with the metrics that moved. Each machine is one dataset: tuned on the first half of every series, scored on the second half, K items a day per machine (`run_smd.py`, `results/smd_eval.csv`):

| Budget K per machine | Incidents caught (196) | Metric movements shown (1,536) | Items / day | False / day | Chance (per movement) |
|---|---|---|---|---|---|
| 5 | 60 (30.6%) | 10.1% | 4.2 | 3.5 | 12.4% |
| 10 | 83 (42.3%) | 19.3% | 7.9 | 6.5 | 12.4% |
| 20 | 108 (55.1%) | 33.9% | 13.8 | 11.0 | 12.4% |

An incident counts once and is caught if any metric that moved was shown while it lasted; the movement column counts every flagged metric on its own. Half of the incidents are found at 20 items a day per machine on real mixed-metric faults, well above the 12.4% chance line, but with low precision: SMD labels only some of the 38 metrics per incident, so a correlated movement on an unlabelled metric counts as false. Where a series had three or more tuning incidents the jump rule learned its own size (Section 4 of `USE_YOUR_DATA.md`). This is a harder benchmark than GAIA and it is reported as it is.

**Same memory fault, three applications (RCAEval RE1).** RE1 injects one memory fault, five times on each of five services, into Online Boutique, Sock Shop and Train Ticket (75 recordings; [RCAEval](https://huggingface.co/datasets/phamquiluan/RCAEval)). Each recording is scored on its own. The first 12 recordings of an application tune the queue; the later 13 are scored once. Those later recordings are then laid end to end, about one day, and the daily cap is applied. Thresholds come from that application's own training recordings (`run_rcaeval.py`, `results/rcaeval_mem.csv`).

The labelled service's memory series does move: median rise 0.51 GB, 0.30 GB and 0.33 GB. With the cap off, the jump rule overlaps 100%, 92.3% and 61.5% of the later injections, each above that rule's chance line (28.5%, 47.1%, 40.9%).

| Application | Memory series | Median rise | Jump episodes on the test recordings | Jump, no cap | Chance | Queue at K = 5 |
|---|---|---|---|---|---|---|
| Online Boutique | 12 | 0.51 GB | 744 | 100% | 28.5% | 0% |
| Sock Shop | 15 | 0.30 GB | 235 | 92.3% | 47.1% | 0% |
| Train Ticket | 64 | 0.33 GB | 1,512 | 61.5% | 40.9% | 0% |

The queue that would page someone catches 0 of 13 faults on every application at K = 2, 5 and 10. On the training recordings the jump rule caught all 12 faults and also raised 679, 220 and 1,323 other episodes. At the usual cost (a miss counts 10, a false episode counts 1) that is more expensive than a rate rule which raises nothing, so the tuner keeps the rate rule. Under the same cap the jump rule still misses: the test day contains hundreds of episodes and five slots, and the slots go to episodes that miss the injection. The fault is visible on all three applications. This budget and this cost leave it off the page.


**Second dataset for the AI component: RCAEval RE2 and RE3 (logs and metrics).** On GAIA the alert classifier adds no catches (§B4), so it is tested again on the 360 RCAEval cases that have logs: RE2 (cpu, memory, disk, delay, packet loss and socket faults; 90 cases per application) and RE3 (code-level faults F1–F5; 30 per application), in Online Boutique, Sock Shop and Train Ticket ([RCAEval](https://huggingface.co/datasets/phamquiluan/RCAEval), MIT licence). `tools/fetch_rcaeval_re23.py` downloads the logs and metrics (about 570 MB; traces are not used) and checks the SHA-256 of all 720 files; `run_rcaeval_logs.py` scores them (`results/rcaeval_logs.csv`, `results/rcaeval_logs.json`).

How the recordings were used (a documented subset, fixed before scoring): cases in start order, the first half trains (classifier, its threshold and the jump confirm gates) and the later half is scored once; each recording is laid on its own on-call day, so K is per case; the fault is the root-cause service from the injection to the end of the recording. Metrics: the product's unit-free jump rule (robust mode) on each service's cpu, memory, disk I/O, socket, p90 latency and error series; leaks and gaps are off, and jumps that start in the first 5 minutes of a recording (a short rolling baseline that live data never has: 43% of all jumps) are dropped. Logs: a line is an alert only at warning or error level (from its level token, or error words when it has none); identical lines from one service within 60 s are one alert. **An alert counts as a catch only if it comes from the root-cause service after the injection**, the same rule as GAIA; on Online Boutique many faults log at the caller (`frontend`) as well as, or instead of, the broken service, so this rule is strict. Every variant shares the same metric items; only the log alerts change, as in §B4. Daily budget K = 10 (2 alert slots; K = 5 and 20 are in the CSV). Rates have 95% Wilson intervals; the difference to "no log alerts" has a 95% bootstrap interval over test cases. The *placebo* column scores the same no-log-alerts queue against a fake fault on the root-cause service in the quiet part of each recording (7 minutes; 10 on RE3 Train Ticket): metric catches by chance are rare.

| Suite | Application | Test cases | No log alerts | Severity | Classifier (95% CI) | Classifier − no log alerts, points (95% CI) | False items / day: none / severity / classifier | Placebo catches | Gate |
|---|---|---|---|---|---|---|---|---|---|
| RE2 | Online Boutique | 45 | 44.4% | 46.7% | 46.7% (32.9%–60.9%) | +2.2 pts (+0.0 to +6.7) | 0.42 / 0.53 / 0.47 | 0 / 45 | refused: saves 3, margin 10 |
| RE2 | Sock Shop | 45 | 84.4% | 84.4% | 86.7% (73.8%–93.7%) | +2.2 pts (+0.0 to +6.7) | 1.42 / 3.38 / 3.13 | 2 / 45 | refused (rules only) |
| RE2 | Train Ticket | 45 | 60.0% | 71.1% | 73.3% (59.0%–84.0%) | +13.3 pts (+4.4 to +24.4) | 3.27 / 3.87 / 3.56 | 2 / 45 | **promoted**: saves 10, margin 10 |
| RE3 | Online Boutique | 15 | 100.0% | 100.0% | 100.0% (79.6%–100.0%) | +0.0 pts (+0.0 to +0.0) | 0.53 / 1.60 / 0.53 | 0 / 15 | refused (rules only) |
| RE3 | Sock Shop | 15 | 93.3% | 93.3% | 93.3% (70.2%–98.8%) | +0.0 pts (+0.0 to +0.0) | 4.07 / 5.40 / 4.67 | 1 / 15 | refused (rules only) |
| RE3 | Train Ticket | 15 | 100.0% | 100.0% | 100.0% (79.6%–100.0%) | +0.0 pts (+0.0 to +0.0) | 1.93 / 3.67 / 1.93 | 0 / 15 | refused (rules only) |

What this shows, with only 45 (RE2) or 15 (RE3) test cases per application:

- **The classifier does add catches on one application.** On RE2 Train Ticket, where faults leave error logs on the broken service, it catches 6 faults the metric items miss and loses none: +13.3 points, interval +4.4 to +24.4. Severity alerts gain 5 cases, with more false items a day (3.87 against 3.56). On RE2 Online Boutique and Sock Shop it adds one case each (interval includes 0).
- **On RE3 the metric items already catch 93–100%** of the code faults at K = 10, so log alerts cannot add catches there; the classifier keeps the false items at the metric-only level on two applications, where severity adds 1.1–1.7 a day.
- **Sock Shop logs are noisy:** both alert variants add 1.7–2.0 false items a day on RE2 (the classifier 1.71, severity 1.96).
- **The promotion gate decides per application, as designed.** With the margin of one missed fault (10, §9) it promotes the classifier only on RE2 Train Ticket. There the queue with its alerts catches 32 of 45 held-out faults with 212 false items, against 32 with 222 by severity and 28 with 198 without log alerts: cost 342 against 352 and 368, a saving of 10, **exactly the margin**. So against what would run instead (severity alerts) the classifier buys no extra catch on the gate's window, only 10 fewer false items in 45 days; the +13.3 points in the table are against no log alerts. A margin above one missed fault would refuse it too. On RE2 Online Boutique, which the gate promoted before the margin, the queue with its alerts costs 283 against 286 by severity and 291 without log alerts (18 of 44 held-out faults with 23 false items; 18 with 26; 17 with 21): a saving of 3, below the margin, so it is kept as a candidate. The gate refuses it on RE2 Sock Shop (more false items than it buys) and on all of RE3 (no catch to buy). The gate splits at the first alert's time of day, so on RE2 Online Boutique and RE3 Online Boutique and Train Ticket its held-out window starts after one test case's injection (one held-out fault fewer); its queue check also keeps the early-recording jumps, which cost all three of its queues the same slots.

Caveats: small test sets (the 95% intervals are 20–30 points wide); one train/test split per application; recordings are short (24–30 minutes) and replayed one per day, which is not a live stream; labels are the dataset's root-cause service, so a caller's error log counts as noise; the level rule for logs without a level token is a keyword list. The classifier's value is therefore **shown on one of six application suites, with a confidence interval that excludes zero** against no log alerts, and the gate keeps it out where it does not pay; against severity alerts its gain there is small (no extra catch on the gate's window).

---

## B6. What "success" means on this data

`run_stats.py` splits the 1,223 test faults by the signal they left:

| Signal | Count | Share | What can catch it |
|---|---|---|---|
| Jump, no log | 868 | **71.0%** | Metric rule only |
| Log and jump | 185 | 15.1% | Either path |
| Log, no jump | 34 | 2.8% | Alerts only |
| No log, no metric data (monitoring gap) | 132 | 10.8% | Nothing — shown as a gap item |
| No log, data but no jump | 4 | 0.3% | Nothing |

- **Where metric data exists, the jump rule overlaps 99.6% of faults** (1,053 / 1,057).
- 170 test faults start inside a monitoring gap. At K = 20 the gap items shown cover 159 of them (all 170 at K = 50).
- **False jumps are not mislabelled faults.** 24.6% of the 256 false jumps start within 10 minutes of a fault on another service; random times do so 27.2% of the time. There is no sign that GAIA logged faults under the wrong service.
- A log-only classifier is capped near **17.9%** (the faults that leave any log). That is why the early 95% target was wrong.

| Early target | Measured | Verdict |
|---|---|---|
| 95% detection at K=20 | 69.6–72.6% (ceiling 84.9%) | Not possible at K=20: more faults per day than slots. 82–86% of the ceiling |
| Beat the usual rule | Detector +3.7 points over a rate rule, 8/8 weeks; full queue +0.7 points, 4 of 8 weeks; log severity 0% | Met by the detector, against a fair rule; the full queue ties it |
| Fewer false items | 1.0–1.8 vs 2.1 a day (rate rule) | Met |
| Works on every service | Jump overlap 81–91% on all ten | Met |
| Walk-forward not lucky | Means 71.3–74.8%, CI 64.5–76.7% | Met |
| Real-time | Confirmed in 0.72 min; no look-ahead, checked by `tests/test_causality.py` | Met |
| Works beyond 1 GB faults | 0.5–2 GB steps and spikes ≥ 98%; leaks 73–99% with the leak rule; 0.25 GB not | Partly — stated |

Per service (test period):

| Service | Faults | With a log | With a jump |
|---|---|---|---|
| dbservice1 | 129 | 0.8% | 91.5% |
| dbservice2 | 148 | 2.0% | 81.1% |
| logservice1 | 126 | 2.4% | 87.3% |
| logservice2 | 108 | 1.8% | 87.0% |
| mobservice1 | 124 | 84.7% | 83.9% |
| mobservice2 | 116 | 87.1% | 87.1% |
| redisservice1 | 111 | 2.7% | 82.0% |
| redisservice2 | 114 | 0.0% | 86.0% |
| webservice1 | 122 | 0.8% | 85.2% |
| webservice2 | 125 | 0.0% | 90.4% |

---

# Part C — Reproduce and deliverables

## C1. How to reproduce

```powershell
python tools/fetch_gaia.py        # needs internet, ~1.8 GB download; checks SHA-256
cd app
$env:PYTHONPATH = (Get-Location).Path
python -m pytest -q
python run_memory.py --metrics data/gaia_metrics --gaia-run data/gaia_raw/run/run --sweep
python run_queue_eval.py
python run_classifier_ablation.py
python run_walkforward.py
python run_stats.py
python run_robustness.py
python ../tools/fetch_nab.py      # needs internet, ~15 MB
python run_nab.py
python ../tools/fetch_smd.py      # needs internet, ~240 MB
python run_smd.py                # 28 machines, about 15 min; resumes if interrupted
python ../tools/fetch_rcaeval_mem.py   # needs internet; RE1 memory cases only
python run_rcaeval.py            # three applications, a few minutes
python ../tools/fetch_rcaeval_re23.py  # needs internet, ~570 MB; RE2/RE3 logs + metrics, SHA-256 checked
python run_rcaeval_logs.py       # RE2 and RE3, about 10 minutes on an 8-core Linux box (9.7 on 11 October); Train Ticket RE2 needs several GB of RAM
python perf/resources.py         # time, peak memory and disk of one GAIA job (§7)
python make_figures.py
python run_validate.py
```

Needs the GAIA files at `app/data/gaia_raw/run/run` and `app/data/gaia_metrics` (not in git; they are large). `python tools/fetch_gaia.py` (from the repository root) downloads them from the GAIA MicroSS dataset (https://github.com/CloudWise-OpenSource/GAIA-DataSet, `MicroSS/run/run.zip` and the split archive `MicroSS/metric/metric_split.*`, about 1.8 GB), extracts the two run tables and the 37 `docker_memory_usage_total` series into that layout, and checks each file's SHA-256 against the copy used for these results (`--check` verifies files already in place). Without GAIA: `python examples/make_example.py`, then `python -m product.cli score --alerts examples/alerts.csv --metrics examples/metrics.csv`.

A faster check without GAIA: `cd app && uv sync --group dev && uv run pytest -q && uv run python run_validate.py` (the result files are in git). Every experiment run is logged in `app/results/experiments.jsonl` (`docs/mlops.md`).

## C1a. Use of AI tools

AI coding assistants were used while building this project: for code review, refactoring, test writing and drafting documentation (visible in the repository history as the `claude/*` branches merged through pull requests #2 to #5, and in `AGENTS.md`). The runtime system uses no generative AI or external AI service. Every number in this report comes from the scripts in `app/` and is checked against `app/results/` by `run_validate.py`; the test suite, lint, type checks and the dependency audit run in CI. Design decisions, the evaluation method and the claims made here are the author's responsibility.

---

## C2. Deliverables

| Item | Where |
|---|---|
| Design | `design_specification.md` |
| Requirements and traceability | `docs/requirements.md` |
| Architecture diagrams | `docs/architecture.md` |
| Design decisions | `docs/adr/` |
| Threat model | `docs/threat_model.md` |
| Test plan and CI gates | `docs/test_plan.md`, `.github/workflows/ci.yml` |
| Deployment, environments, AI governance | `docs/deployment.md`, `app/deploy/*.env` |
| MLOps: versioning, updates, experiment tracking | `docs/mlops.md`, `app/results/experiments.jsonl` |
| Monitoring and evaluation | `docs/monitoring.md`, `/metrics`, `/monitoring` |
| Privacy and responsible AI | `docs/privacy_responsible_ai.md` |
| Performance results | `app/perf/load_test.py`, `app/results/perf/` |
| Retrospective | `docs/retrospective.md` |
| Change history | `CHANGELOG.md` |
| Plan / roadmap | `project_plan.md`, `ROADMAP.md` |
| Validation record | `VALIDATION.md` |
| Review of the earlier version | `CODE_REVIEW.md` |
| This report | `FINAL_REPORT.md` |
| Code + tests | `app/` |
| Result tables | `app/results/` |
| Figures | `figures/detection_vs_k.svg`, `figures/walkforward.svg`, `figures/use_cases.svg` (UML use cases) |
| Tool | Web app `docker compose up` (http://127.0.0.1:8080/, **/evaluate** for your own data), CLI `python -m product.cli` |
| End-user guide | `USE_YOUR_DATA.md` |

---

## Checked numbers

`python run_validate.py` (in `app/`) recomputes every value below from `app/results/` and fails if one no longer rounds to what is printed here, if a key is missing, or if a sentence it guards (for example "the detector wins 8/8 weeks") stops being true. Rates are shares of test faults; `false` is false items a day.

| Key | Value | Where in this report |
|---|---|---|
| `queue.K20.logs_severity` | 0.0% | §B1 table |
| `queue.K20.static_p99` | 57.2% | §B1 table |
| `queue.K20.delta_5m` | 68.9% | §B1 table |
| `queue.K20.jumps_online` | 67.8% | §B1 table |
| `queue.K20.jumps_tuned` | 72.6% | §B1 table |
| `queue.K20.proposed` | 69.6% | §B1 table |
| `queue.K20.jumps_hindsight` | 72.9% | §B1 table |
| `queue.K50.proposed` | 85.8% | §B1 text |
| `false.K20.delta_5m` | 2.1 | §B1 table |
| `false.K20.jumps_tuned` | 1.0 | §B1 table |
| `false.K20.proposed` | 1.8 | §B1 table |
| `ceiling.K20` | 84.9% | §B1 |
| `misses.K20.no_metric_data` | 166 | §B1 misses |
| `misses.K20.budget_full` | 198 | §B1 misses |
| `misses.K20.no_jump` | 4 | §B1 misses |
| `misses.K20.below_threshold` | 4 | §B1 misses |
| `walkforward.proposed.mean` | 71.3% | §B2 |
| `walkforward.proposed.ci_low` | 64.5% | §B2 |
| `walkforward.proposed.ci_high` | 76.7% | §B2 |
| `walkforward.proposed.wins` | 4 | §B2 |
| `walkforward.jumps_tuned.mean` | 74.8% | §B2 |
| `walkforward.jumps_tuned.wins` | 8 | §B2 |
| `walkforward.delta_5m.mean` | 70.7% | §B2 |
| `robustness.step_1g.jump_0.5g` | 100% | §B3 |
| `robustness.step_0.25g.jump_0.5g` | 1% | §B3 |
| `robustness.ramp_1g.jump+leak_3h` | 99% | §B3 |
| `robustness.step_0.5g.jump_0.5g+threshold` | 0% | §B3 |
| `ablation.K20.classifier` | 69.6% | §5, §B4 |
| `ablation.K20.severity` | 71.0% | §5, §B4 |
| `ablation.K20.no_alerts` | 71.2% | §5, §B4 |
| `ablation.false.K20.classifier` | 1.8 | §5, §B4 |
| `ablation.false.K20.severity` | 1.4 | §5, §B4 |
| `ablation.false.K20.no_alerts` | 1.2 | §5, §B4 |
| `queue.K20.p3_ranker` | 69.7% | §B4 |
| `queue.K20.p3_grouped` | 70.7% | §B4 |
| `queue.K20.p3_per_series` | 69.3% | §B4 |
| `ablation.attached.K20.classifier` | 280 | §B4 |
| `severity.K20.fault_alerts` | 507 | §B4 |
| `severity.K20.fault_alerts_warning` | 471 | §B4 |
| `severity.K20.fault_alerts_info` | 36 | §B4 |
| `severity.K20.escalated` | 44 | §B4 |
| `severity.K20.escalated_with_fault` | 0 | §B4 |
| `severity.K20.escalated_days` | 4 | §B4 |
| `severity.K20.cut_by_daily_budget` | 14 | §B4 |
| `severity.K20.escalated_while_shown_jump_open` | 0 | §B4 |
| `severity.K20.minutes_to_shown_metric_min` | 199 | §B4 |
| `severity.K20.minutes_to_shown_metric_median` | 617 | §B4 |
| `severity.K20.alerts_shown` | 11 | §B4 |
| `gate.held_out.fault_alerts_found` | 339 | §B4 |
| `gate.queue.faults` | 1223 | §5, §B4 |
| `gate.queue.caught_with_model` | 848 | §5, §B4 |
| `gate.queue.caught_no_log_alerts` | 872 | §5, §B4 |
| `gate.queue.false_with_model` | 95 | §B4 |
| `gate.queue.false_no_log_alerts` | 58 | §B4 |
| `gate.queue.caught_fallback` | 871 | §B4 |
| `gate.queue.false_fallback` | 70 | §B4 |
| `gate.margin` | 10 | §9, §B4 |
| `ablation.K20.shipped` | 71.0% | §B1 table |
| `ablation.false.K20.shipped` | 1.4 | §B1 table |
| `queue.K50.p3_per_series` | 84.1% | §B4 |
| `false.K20.p3_per_series_jumps` | 0.8 | §B4 |
| `queue.K20.p3_carryover` | 72.6% | §B4 |
| `queue.K20.delta_5m_carryover` | 72.5% | §B4 |
| `walkforward.carryover.mean` | 74.8% | §B4 |
| `walkforward.delta_carryover.mean` | 74.3% | §B4 |
| `nab.realAWSCloudwatch` | 71% | §B5 |
| `nab.realAdExchange` | 83% | §B5 |
| `nab.realKnownCause` | 38% | §B5 |
| `nab.realTraffic` | 36% | §B5 |
| `nab.realTweets` | 36% | §B5 |
| `smd.K5.incidents` | 30.6% | §B5 |
| `smd.K10.incidents` | 42.3% | §B5 |
| `smd.K20.incidents` | 55.1% | §B5 |
| `smd.K5.movements` | 10.1% | §B5 |
| `smd.K10.movements` | 19.3% | §B5 |
| `smd.K20.movements` | 33.9% | §B5 |
| `rcaeval.online-boutique.rise_gb` | 0.51 | §B5 |
| `rcaeval.online-boutique.jump_spikes` | 744 | §B5 |
| `rcaeval.online-boutique.jump_open` | 100% | §B5 |
| `rcaeval.online-boutique.jump_chance` | 28.5% | §B5 |
| `rcaeval.online-boutique.jump_train_false` | 679 | §B5 |
| `rcaeval.online-boutique.K5` | 0% | §B5 |
| `rcaeval.sock-shop.rise_gb` | 0.30 | §B5 |
| `rcaeval.sock-shop.jump_spikes` | 235 | §B5 |
| `rcaeval.sock-shop.jump_open` | 92.3% | §B5 |
| `rcaeval.sock-shop.jump_chance` | 47.1% | §B5 |
| `rcaeval.sock-shop.jump_train_false` | 220 | §B5 |
| `rcaeval.sock-shop.K5` | 0% | §B5 |
| `rcaeval.train-ticket.rise_gb` | 0.33 | §B5 |
| `rcaeval.train-ticket.jump_spikes` | 1512 | §B5 |
| `rcaeval.train-ticket.jump_open` | 61.5% | §B5 |
| `rcaeval.train-ticket.jump_chance` | 40.9% | §B5 |
| `rcaeval.train-ticket.jump_train_false` | 1323 | §B5 |
| `rcaeval.train-ticket.K5` | 0% | §B5 |
| `stats.jump_rate_with_data` | 99.6% | §B6 |
| `stats.chance_overlap` | 2.6% | §B1 |
| `stats.confirm_minutes_median` | 0.72 | §B1 |
| `stats.false_jumps_near_fault` | 24.6% | §B6 |
| `resources.wall_seconds` | 54.9 | §7 resources |
| `resources.peak_rss_mb` | 505 | §7 resources |
| `resources.peak_rss_mb_before_job` | 173 | §7 resources |
| `resources.db_mb` | 13.0 | §7 resources |
| `resources.db_kb_per_day` | 215 | §7 resources |
| `rcalogs.RE2.ob.classifier` | 46.7% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.classifier.ci_high` | 60.9% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.classifier.ci_low` | 32.9% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.diff.ci_high` | 6.7% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.diff.ci_low` | 0.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.false.classifier` | 0.47 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.false.none` | 0.42 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.false.severity` | 0.53 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.gate.caught_fallback` | 18 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.gate.caught_no_log_alerts` | 17 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.gate.caught_with_model` | 18 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.gate.false_items_fallback` | 26 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.gate.false_items_no_log_alerts` | 21 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.gate.false_items_with_model` | 23 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.gate.faults` | 44 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.gate.saving` | 3 | §9, §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.none` | 44.4% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.placebo` | 0 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.severity` | 46.7% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ob.test_cases` | 45 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.classifier` | 86.7% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.classifier.ci_high` | 93.7% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.classifier.ci_low` | 73.8% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.diff.ci_high` | 6.7% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.diff.ci_low` | 0.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.false.classifier` | 3.13 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.false.none` | 1.42 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.false.severity` | 3.38 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.none` | 84.4% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.placebo` | 2 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.severity` | 84.4% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.ss.test_cases` | 45 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.classifier` | 73.3% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.classifier.ci_high` | 84.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.classifier.ci_low` | 59.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.diff.ci_high` | 24.4% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.diff.ci_low` | 4.4% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.false.classifier` | 3.56 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.false.none` | 3.27 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.false.severity` | 3.87 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.gate.caught_fallback` | 32 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.gate.caught_no_log_alerts` | 28 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.gate.caught_with_model` | 32 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.gate.false_items_fallback` | 222 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.gate.false_items_no_log_alerts` | 198 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.gate.false_items_with_model` | 212 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.gate.faults` | 45 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.gate.saving` | 10 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.none` | 60.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.placebo` | 2 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.severity` | 71.1% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE2.tt.test_cases` | 45 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.classifier` | 100.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.classifier.ci_high` | 100.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.classifier.ci_low` | 79.6% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.diff.ci_high` | 0.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.diff.ci_low` | 0.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.false.classifier` | 0.53 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.false.none` | 0.53 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.false.severity` | 1.60 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.none` | 100.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.placebo` | 0 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.severity` | 100.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ob.test_cases` | 15 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.classifier` | 93.3% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.classifier.ci_high` | 98.8% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.classifier.ci_low` | 70.2% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.diff.ci_high` | 0.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.diff.ci_low` | 0.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.false.classifier` | 4.67 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.false.none` | 4.07 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.false.severity` | 5.40 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.none` | 93.3% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.placebo` | 1 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.severity` | 93.3% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.ss.test_cases` | 15 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.classifier` | 100.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.classifier.ci_high` | 100.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.classifier.ci_low` | 79.6% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.diff.ci_high` | 0.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.diff.ci_low` | 0.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.false.classifier` | 1.93 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.false.none` | 1.93 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.false.severity` | 3.67 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.none` | 100.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.placebo` | 0 | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.severity` | 100.0% | §B5 RCAEval RE2/RE3 |
| `rcalogs.RE3.tt.test_cases` | 15 | §B5 RCAEval RE2/RE3 |
| `rcalogs.early_jump_share` | 43% | §B5 RCAEval RE2/RE3 |
| `rcalogs.gate.margin` | 10 | §9, §B5 RCAEval RE2/RE3 |
