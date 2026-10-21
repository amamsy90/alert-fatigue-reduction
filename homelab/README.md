# Home lab

A single Linux machine runs three simulated companies and feeds the on-call
queue in **shadow mode**. The lab collects the alerts, memory series, logs and
a sample of traces itself, and it writes the chaos schedule down as ground
truth. Evaluation reads those files. It does not read the app's SQLite buffer,
because ingest deduplicates and the buffer is not the historical record.

Nothing in this tree turns Live paging on. `LEARNED_RANKER`, `METRIC_MODEL`
and `LLM_EXPLAIN` stay off. The app has no tenant field, so company tags and
per-company budgets live in the collector and the evaluation script. The
running app still has one shared daily budget.

The numbers below that describe RAM, CPU and disk are **estimates**. They were
not measured on ai-ROG. Detection rates are not in this repository. `scripts/evaluate.sh`
writes them only after a real collection, and a run that cannot score says so
instead of filling in a rate.

## Companies

`companies.yaml` is the list. The default three are enabled:

| id | app | namespace | load | alerts | chaos | RAM (estimate) | CPU (estimate) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| acme | Online Boutique v0.10.7 | `company-acme` | steady | noisy | pod-kill, network delay, CPU stress, every 20–45 min | 1.5–2.5 GB | 0.5–1.5 cores |
| borealis | OpenTelemetry Demo chart 0.42.5, reduced | `company-borealis` | daily peaks | strict | network loss, memory stress, HTTP abort, every 30–60 min | 3–5 GB | 1–2.5 cores |
| cassini | Sock Shop, pinned commit | `company-cassini` | bursty | noisy | pod-kill, network delay, CPU stress, every 25–55 min | 1.5–2.5 GB | 0.5–1.5 cores |

Shared Prometheus, Loki, Tempo, the collector, Alloy, Chaos Mesh and the app
are another **5–8 GB of RAM and 1–2 cores**, as an estimate. All three
companies plus that stack is about **12–18 GB of RAM and 4–8 cores**. A laptop
with about 20 GB free is the comfortable floor; 32 GB is an easier fit. The
RTX 5090 is not used by the lab. The queue ranker in this evaluation is the
CPU pointwise model.

Disk, still an estimate: Prometheus is capped at 20Gi / 15 days, Loki at 10Gi
/ 72 hours, Tempo at 15Gi / 48 hours, plus several GB of images. Leave
**50–80 GB free** before `up.sh`. The memory CSV is one summed row per service
per minute. At a dozen services and three companies that is on the order of
tens of megabytes a day, not a measurement.

`draco` is DeathStarBench's social network and it is **disabled**. Mongo,
Redis, Memcached and the media services do not fit beside the three apps and
the observability stack. The estimate in the yaml is 4–6 GB and 1–2 cores, and
it should be run alone.

## Prerequisites

On ai-ROG (Ubuntu-like, Docker already installed):

- Docker
- [k3d](https://k3d.io/) v5
- kubectl
- Helm 3
- curl
- `uv` (the app toolchain; the scripts call `uv run --with pyyaml`)
- systemd, so `systemd-inhibit` can ask logind not to idle-sleep

`./scripts/up.sh --dry-run` and `./scripts/render.sh` do not need Docker, k3d,
Helm or kubectl. They only render files under `homelab/data/render/`.

## One command

From this directory:

```bash
cd homelab
./scripts/up.sh
```

That creates the k3d cluster `oncall-homelab` (one server, no agents, Traefik
disabled) if it is missing, installs kube-prometheus-stack, Loki, Tempo, an
OpenTelemetry collector and Alloy, installs Chaos Mesh, deploys each enabled
company, builds `oncall-queue:homelab` from `app/` and imports it into k3d, and
checks that `/connectors` reports shadow mode with no paging channel. It then
starts the chaos scheduler, the collector and a score loop. The score loop
posts `{"mode":"ingest","budget_k":20,"shadow_mode":true}`.

Port-forwards bind to `127.0.0.1` only:

| local | service |
| --- | --- |
| 8080 | the app |
| 9090 | Prometheus |
| 9093 | Alertmanager |
| 3100 | Loki |
| 3200 | Tempo |
| 3000 | Grafana (`admin` / `homelab`) |

The Grafana password is a localhost lab default, committed on purpose. Change
`grafana.adminPassword` in `config/kps-values.yaml` if anything other than you
can open that port. Do not set `APP_PASSWORD`, `INGEST_TOKEN`,
`ALLOW_OPEN_ACCESS`, a paging key, or `PROM_URL`.

A second `./scripts/up.sh` upgrades in place and resumes. Chaos state is
`homelab/data/chaos-state.json`. The collector's watermark is
`homelab/data/collect-state.json`.

## Check that it is working

```bash
./scripts/status.sh
```

That prints workload pods, Chaos Mesh experiments, the current firing-alert
count from Prometheus, the shadow check, and the row counts in
`data/collected/alerts.csv` and `data/faults.csv`. There is no expected alert
rate in this README. The noisy companies are meant to be chatty; the strict
company is not. Read the count from the command.

`kubectl -n observability port-forward` is already started by `up.sh`. The app
UI is http://127.0.0.1:8080 . The page shows what the queue would have done.
It does not page.

## Stop, resume, tear down

```bash
./scripts/down.sh
```

stops the scheduler, the collector, the score loop and the port-forwards. The
cluster and `homelab/data/` stay.

```bash
./scripts/up.sh
```

resumes them.

```bash
./scripts/teardown.sh
```

deletes the k3d cluster and deletes `homelab/data/` (telemetry, ground truth
and renders).

## Sleep

The long-running processes are wrapped in `systemd-inhibit --what=idle:sleep`.
Closing the lid can still suspend the machine, because the lid switch is a
separate logind action. If that happens, run `./scripts/up.sh` again. The
scheduler closes an overdue fault as `truncated` or `closed` from the saved
clock and waits out the rest of the gap. The collector asks Prometheus for the
range it missed, back to the retention window on a first run.

## Fewer companies

Pass a comma-separated list, or set `enabled: false` in `companies.yaml`:

```bash
./scripts/up.sh --companies acme
./scripts/up.sh --companies acme,cassini
```

Borealis is the expensive one. Acme alone is the smallest useful lab.

## Add a company

1. Add an object to `companies.yaml`. `id` is a lowercase slug. `namespace`
   defaults to `company-<id>` if you omit it.
2. Set `app` to `online-boutique`, `otel-demo`, `sock-shop`, or `manifest`.
   A `manifest` company needs `manifest_url` before `enabled: true`. The URL
   must apply into `namespace`. Enabling `draco` without a URL is an error.
3. Pick `load.profile` (`steady`, `daily`, `bursty`), `alerts.sensitivity`
   (`noisy` or `strict`) and a chaos mix. The gap between fault starts has to
   sit inside 20–60 minutes, and the fault duration has to be shorter than
   that gap.
4. Put an RAM and CPU estimate on the object. Say it is an estimate.
5. Render, or bring the lab up:

```bash
./scripts/render.sh
./scripts/up.sh --companies <id>
```

Alert rules, the collector's company attribute and the chaos selector are
generated from the file. You do not edit the app. For a manifest app, label
the pod template with `company: <id>` and `service: <catalog name>` yourself
if the lab does not already know that app's deployment names. Chaos Mesh
selects pods, not Deployment objects.

DeathStarBench social network is the worked example of a company that should
not be enabled next to the others. Run it alone once you have a manifest URL:

```bash
./scripts/up.sh --companies draco
```

## What each company runs

- **Acme** applies the Online Boutique release manifest. The release file has
  no collector address, so the lab sets `ENABLE_TRACING`,
  `COLLECTOR_SERVICE_ADDR` and `OTEL_RESOURCE_ATTRIBUTES=company=acme` on the
  app deployments. The collector also sets `company` from the Kubernetes
  namespace when the span does not already have it.
- **Borealis** installs the OpenTelemetry demo Helm chart with its own
  collector, Jaeger, Prometheus, Grafana and OpenSearch turned off, and with
  Kafka, accounting, fraud-detection and the other heavy components turned
  off. The generated values also clear checkout's `wait-for-kafka` init
  container and set `KAFKA_ADDR` empty. Checkout 3.1.0 then starts and skips
  the order bus; cart, payment, shipping, and email still run. Accounting and
  fraud-detection stay off with Kafka. To put the bus back, remove those
  overrides and the `kafka: enabled: false` entry in the generated values.
  `frontend-proxy` stays on as the shop entry. It still names the disabled UI
  hosts (Grafana, Jaeger, image provider); those routes fail, and the shop
  route is `frontend`.
  Load follows the clock: 1 replica overnight, 2 through the work day, 3 at
  11:00–13:00 and 18:00–20:00 UTC.
- **Cassini** applies Sock Shop at commit `5193b1e…` into `company-cassini`
  and adds a small curl load generator. The shop does not emit OTLP traces.
  Its latency and error rules stay quiet. Noise comes from CPU, memory,
  restarts and the kube-prometheus workload rules. Bursty load is 4 replicas
  for 10 minutes when the hour is divisible by 3 and the minute is under 5,
  otherwise 1 replica.

Chaos Mesh 2.7.1 uses containerd. The chart values point at
`/run/k3s/containerd/containerd.sock`. Some k3d builds expose
`/run/containerd/containerd.sock` instead. If the chaos daemon cannot see the
runtime, rerun with:

```bash
HOMELAB_CONTAINERD_SOCKET=/run/containerd/containerd.sock ./scripts/up.sh
```

One experiment named `homelab` runs per company. The next one starts 20–60
minutes after the previous start, not after it ends. Pod kill is recorded as
**two minutes**, because the process is only down until Kubernetes restarts
it. The manifest duration is still the drawn 3–6 minutes, and the scheduler
deletes the experiment when the recorded window ends. Other faults are
recorded for the experiment's own duration. Only rows with `status=closed`
are scored. A fault that was still open when the laptop stopped is
`truncated` and is not scored.

## Observability

kube-prometheus-stack 92.11.0 (release name `kps`) keeps the default workload
and node rules. etcd, the scheduler, the controller manager and kube-proxy
scrapes and rules are off, because k3s embeds them and those targets would be
the noise source for the wrong reason. Extra `PrometheusRule` objects add
per-service latency, error ratio and restarts. Noisy companies also alert on
CPU and memory. Strict companies do not. The latency and error rules use the
collector's span metrics `traces_spanmetrics_duration_milliseconds_bucket` and
`traces_spanmetrics_calls_total`.

Every homelab alert carries a constant `company` label and a `service` label
of the form `company/service`. CPU and restart series do not have a company
label of their own, so the company id is written into the rule. Alerts that
are not in a company namespace are tagged `cluster`. They are included in the
pooled evaluation and left out of the per-company scores.

Alertmanager posts to
`http://oncall-queue.observability.svc.cluster.local:8080/ingest/alertmanager`
with `send_resolved: false` and without `score_now`. The app runs inside the
cluster, so the client address is a pod IP. `ALLOW_OPEN_ACCESS` stays `0`.
There is no `APP_PASSWORD`, so the Live switch in the UI is refused. Do not
reuse a host volume that already has an `ops.json` with Live saved; the lab
PVC is new.

The collector exports span metrics for every span and forwards a **10%**
sample of traces to Tempo. It does not remote-write the apps' own metrics.
Alloy tails logs in `company-*` namespaces only.

Pins live in `config/versions.env`. The GitHub release pages for Loki 3.4.2,
Tempo 2.7.2, Alloy v1.8.1 and collector v0.128.0 existed on 2026-10-21. A
laptop `docker pull` is the check that the registry tag still resolves.

## Evaluation

After the series is longer than seven days, and at least one closed fault
starts after that first week:

```bash
./scripts/evaluate.sh
./scripts/evaluate.sh --learned-ranker
```

Results go to `homelab/results/<utc>/`:

- `summary.json` — per company and pooled: scored or not, and why
- `per_company/<id>/queue_eval.csv` and `miss_causes.csv` when that company scored
- `pooled/` — the same, all companies together, including `cluster` rows
- `leave_one_company_out.json`

The queue path is `run_window` / `score_window`, the same one as
`app/run_queue_eval.py`. Train is the first `split_day` days (7). Test is the
later days. Rows are not shuffled. If the span is not longer than seven days,
or no closed fault starts in the test window, or the evaluator refuses the
sample (it needs both fault and noise alerts in week 1, and both a real jump
and a false jump), the output is `scored: false` and a reason. No detection
rate is written.

Service names in the evaluation are `company/service`, so `frontend` in Acme
does not collide with `frontend` in Borealis. Pod names are mapped back to
the demo's service name before that prefix is added. Helm's
`borealis-frontend-<replica>` becomes `borealis/frontend`, which is what the
ground-truth row says. The live webhook may still show a raw pod prefix; the
evaluation uses the collector CSVs.

Memory jumps use **relative** mode at ratio **0.3**, and the 5-minute delta
baseline uses **50 MB** (`memory_jump_bytes`). Those are recorded in
`summary.json`. The GAIA absolute gate of 0.5 GB would almost never fire on
these containers. This is the existing config field, not a new model.

`p3_ranker` inside `run_window` is the repo's jump ranker. It is not the
`LEARNED_RANKER` product flag. That flag stays off.

`--learned-ranker` is opt-in and does **not** set `LEARNED_RANKER`,
`METRIC_MODEL` or `LLM_EXPLAIN`. For each held-out company it trains
`fit_ranker` (pointwise, CPU, seed 42) on the other companies' items and
faults strictly before `held-out origin + 7 days`. It does not use the
held-out company's labels, and the confirm-rise gate is frozen on the other
companies' pre-boundary jumps. Both the baseline queue and the ranker see that
same candidate list. At K=20 it keeps the ranker only when the 95% bootstrap
interval on the per-fault catch difference has lower bound greater than 0
(`bootstrap_mean_ci`, 4000 draws, seed 42). That is the rule in
`src/model_eval.py`. If the training sample is one class, the decision is
`skipped` and `per_fault_catch_delta` is null. The default, without the flag,
is `status: off` and no interval.

The product's daily budget stays shared. Per-company K and the pooled K are
applied only in this offline script.

## Methodology and threats to validity

The lab is a place to collect a labelled series, not a claim that three
namespaces are three companies.

- The companies share one machine, one kernel and one disk. A CPU or memory
  fault in one namespace moves latency in the others. Leave-one-company-out
  therefore does not separate independent organisations.
- They share Prometheus, Loki, Tempo, Chaos Mesh and the same rule families.
  A strict company and a noisy company differ by thresholds, not by tooling.
  A model can learn the lab instead of learning incidents.
- Load is a script. It is not users. Daily peaks are the UTC clock, not a
  real shop.
- Sock Shop emits no application traces, so its error and latency alerts do
  not fire. Comparing it with the two traced shops mixes two observability
  shapes.
- Pod-kill ground truth is a two-minute restart assumption. The real outage
  is usually shorter. Network, stress and HTTP faults are recorded for the
  whole experiment, including time the service may already have recovered.
- The span file is a 10% sample, then one page of recent Tempo traces per
  collector pass. It is not a complete trace archive. The headline evaluation
  does not score a trace model. `METRIC_MODEL` needs torch and stays off, so
  this script does not invent a metric-model rate.
- Cluster alerts and kube-prometheus defaults are real noise, and they are
  also a shared signal across companies.
- The in-app queue is one tenant. Per-company budgets exist only offline. A
  shadow page that mixes companies is what the UI shows.
- Time order is preserved inside each company. The held-out boundary is that
  company's own origin plus seven days, not a calendar week that the other
  companies necessarily share.

## What is checked without a cluster

CI renders the manifests, runs shellcheck, ruff and these unit tests. It does
not start k3d, Helm or a collector. A green CI run does not mean the charts
install, the images pull, or the chaos socket path matches your k3d node.
Those happen on the laptop.
