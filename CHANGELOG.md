# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/). Dates are commit dates.

## [1.2.2] - 2026-11-07

### Fixed
- The release workflow could not start: `aquasecurity/trivy-action@0.28.0` no longer resolves (its tags now carry a `v`). It is pinned to the commit of `v0.28.0`. Release 1.2.1 was tagged but stopped at that step and published nothing; its checks all passed. 1.2.2 has the same code and the fix.

## [1.2.1] - 2026-11-07

### Added
- Live soak: `app/run_live_soak.py` with `app/integration/live/` (a simulated six-service fleet, Prometheus, Alertmanager and the app in Docker Compose). Two real-time two-hour runs: 13 of 19 faults caught, then 18 of 18 after the rig fix, no false jumps, no failed job; a `--drift-phase` that posts alert storms and shows the drift check warning on a shifted mix (`FINAL_REPORT.md` §B9, `app/results/live_soak*.json`, `live_drift_phase.json`). It is a soak test, not a pilot.
- Tests for the soak's matching, the fleet schedule (faults on one service at least 15 minutes apart) and the storm generator.

### Found
- The jump rule cannot see a second fault that starts within its 15-minute median window of an earlier one on the same service. The soak rig now spaces faults 20 minutes apart; the limit is documented, the detector is unchanged.

## [1.2.0] - 2026-11-04

### Changed
- Version 1.2.0: `app/pyproject.toml`, `product.__version__`, this changelog, the current doc headers, the README, and the report. The home lab, the day-12 snapshot, the four-page report, and the optional queue checks are in this release. New flags stay off. Live paging is unchanged.
- On this commit the suite is 450 passed, 10 skipped, line coverage 86%. `run_validate.py` matches 195 checked numbers.
- The four-page report leads with the budgeted jump queue: at K = 20, 72.6% against 68.9% for the Prometheus rate rule and 0% for log severity, the jump detector winning 8 of 8 walk-forward weeks against that rate rule, with fewer false items a day. Ranker KEEP, hybrid, leave-one-company-out, and the optional metric, trace and LLM measurements stay in the appendices. Those flags stay off. Live paging is unchanged.
- The report answers the proposal feedback and adds the day-12 homelab evaluation (`FINAL_REPORT.md` §B7, `proposal.md`, `docs/results/homelab_day12/`). The GAIA learned ranker re-run is `docs/results/gaia_ranker_rerun/`: at K = 20, 887 against 851 of 1,223, KEEP. `LEARNED_RANKER` stays off.

### Added
- Ranker production audit, all off by default (`RANKER_EXPLAIN`, `RANKER_ABSTAIN`, `RANKER_DISAGREE`). With the flags off the queue, the job JSON and the page set are unchanged. With a flag on, the K queue is still the ranker's selection: explanations and a "needs human review" list are shadow output, and neither list is passed to the pager. `RANKER_EXPLAIN` attaches exact interventional SHAP reasons (the `shap` package is not a dependency; stored trees have no booster, so `pred_contribs` is not used). `RANKER_ABSTAIN` lists dropped items that are not confidently below the score gate. `RANKER_DISAGREE` flags a strong slot disagreement with the contrastive encoder, when that file loads, or with the first-come queue otherwise. An in-queue disagreement stays in the page set. `app/run_ranker_audit.py` writes global importance, worked examples, a hypothetical coverage curve and a disagreement contrast. It does not turn any flag on. Design and the measured synthetic run are in `docs/ML_MODELS.md`. GAIA was not on this machine.

### Measured (synthetic only)
Run from `app/` on the shipped generator (`generate_multivariate`, 21 days, seed 42), pointwise ranker, seeds 42, 0 and 1, walk-forward. File: `app/results/ranker_audit_synthetic.json`. `gaia` is `pending: this run did not score GAIA`. `pages` is false. `flags_changed` is false. The encoder was not loaded (`not requested`), so the second opinion is the first-come queue. Train end `2026-01-12T03:00:00+00:00`, test end `2026-01-25T03:15:01+00:00`, `window_end` `last alert`, 6 held-out faults. SHAP was computed on seed 42 only. `contribution_sum_gap` is 0.0.

- **K = 20, every seed: drop.** Ranker and first-come both caught 2 of 6 (detection 0.3333, false items a day 0.29, precision 0.3333). Per-fault catch difference 0.0 / 0.0 / 0.0, n = 6. Seven candidates, 6 slot items. The tuned threshold is 1.9985752783662702, above every probability, and the day already fits in K, so the shipped review list is empty (delta 1.013912, review count 0) and disagreement count is 0 (`no-contrast`: 0 flagged, 7 other, other catch rate 0.4286). The hypothetical coverage curve is 0.0 at every delta (6 candidates, 0 automated, 6 review). Targets 0.5, 0.7 and 0.9 are not reached. Mean absolute SHAP on the 6 explained rows: `hour_cos` 0.218464, `rise_ratio` 0.064278, `log_rise` 0.017954, `kind_alert` 0.016498, `severity_rank` 0.016498, `kind_jump` 0.016498, `hour_sin` 0.001193, and 0.0 for `metric_score`, `kind_rule`, `kind_leak`, `kind_gap`. Worked examples: highest score alert `db` at `2026-01-13T18:01:00+00:00`, score 0.986852, in queue, catches a fault; lowest score jump `db` at `2026-01-14T01:01:00+00:00`, score 0.412015, in queue, does not. `catches_fault` is a scoring label, not a feature.
- **K = 1, every seed: drop.** This budget is only so the cut binds. Ranker caught 1 of 6 (detection 0.1667, false items a day 0.0, precision 1.0) against a first-come catch of 0 (false items a day 0.14, precision 0.0). Mean difference 0.1667, interval 0.0 to 0.5, n = 6. The lower bound is 0.0, so the keep rule drops it. Shipped preview delta 0.11037, review count 1, and that one review item is a fault the queue missed. Disagreement count 1 (dropped). Flagged catch rate 0.0 (n = 1) against 0.5 on the other 6; difference mean −0.5, interval −0.8333 to 0.0 (n = 7, 2654 draws). The interval includes 0, and 0 of 5 misses carry a disagreement flag. Hypothetical coverage does not hit 0.5, 0.7 or 0.9 within 0.02 (closest 0.6667 at delta 0.1029, accuracy 1.0, precision null; and 0.8333 at delta 0.0, accuracy 1.0, precision 1.0).
- **Walk-forward, K = 20, both folds, every seed: drop.** Fold 1 (train end `2026-01-12T03:00:00+00:00`, test end `2026-01-19T03:00:00+00:00`, 5 faults) caught 2 against 2, interval 0.0 / 0.0 / 0.0, review 0, disagreement 0. Fold 2 (train end `2026-01-19T03:00:00+00:00`, test end `2026-01-25T03:15:01+00:00`, 1 fault) caught 1 against 1, same interval, review 0, disagreement 0.

No keep is claimed. The three flags stay off. The GAIA command is in `docs/ML_MODELS.md` and has not been run.

### Fixed
- Home lab alerts: a still-firing `ALERTS` series was written again on every collector pass, because the series has no 0 sample and each pass's window start became a new episode id. A gap longer than 120 seconds now ends the stretch, and the open episode is stored in `collected/.alerts.episodes.json` so a continuing alert keeps its first start and id. A re-fire after that gap is a new row. `alerts.csv` collected before this fix (the live file had about 87k rows for about 979 episodes) is not usable as-is. Do not delete the live files: the collector only re-reads 7 days. `homelab/rebuild_alerts.py` rebuilds episodes from Prometheus history inside the 15-day retention, in 5-day chunks at a 60-second step, into a separate `alerts.csv`. Copy metrics and faults beside that file and point `HOMELAB_DATA` at the snapshot.

### Added

- `app/run_live_replay.py` and `FINAL_REPORT.md` §B8: an accelerated replay of real alerts (the committed GAIA sample; RCAEval RE2 Train Ticket one recording a day) through ingest, scoring, a simulated engineer's marks and the scheduled retrain. It is a replay, not a pilot. It found and fixed two defects: a retrain that repeated daily on the same marks when no model was active (39 refused candidates in 40 days, now 8; new marks are counted from the last training attempt), and drift that compared the two halves of one day's batch and warned every day (an ingest job is now compared with the alerts earlier jobs stored). 19 more checked numbers (278 in all). Grafana dashboard `app/deploy/grafana/oncall-queue.json`, with a test that every metric it uses is exported by the app.
- A Helm chart (`app/deploy/helm/oncall-queue/`), `tools/smoke_test.py` and `tools/deploy.sh`. The chart keeps shadow mode, a required password, one replica and a hardened pod (non-root, read-only root filesystem, no capabilities); data survives a rollback; an optional retrain CronJob and Prometheus Operator objects. `deploy.sh` installs atomically, smoke-tests and rolls back on failure. The release workflow now scans the image (Trivy), writes an SBOM, checks the tag against the chart and app versions, and deploys the built image to a throwaway kind cluster before publishing. `/health` now reports the version. Found and fixed with a real run of the hardened image: the sample job writes under `/app/data/sample_product`, so it is a writable mount.
- `product/retrain.py`: scheduled retraining (`python -m product.cli retrain`, `POST /models/retrain`). It retrains only when due (5 new Real marks, or an old model with a new mark; 6 hour cooldown; never during a scoring job), through the same promotion gate, then reads the promoted model back from disk and scores real alerts with it; a failure restores the previous version automatically. Model cards now also record the trigger, the batch window and the library versions. New gauges `oncall_model_age_days`, `oncall_model_retrain_due`, `oncall_retrain_total{outcome}` and two alert rules.
- `src/challenger.py` and `product/model_store.py`: champion and challenger in shadow. A model refused by the promotion gate is kept as a candidate; on every later job the newest candidate also decides the same live alerts, and its labelled-alert cost (a miss costs 10, a false item 1) is tallied against the active model's in the registry card. After 3 jobs and 30 labelled alerts it recommends promote only if it saves at least the gate's margin and wins more jobs than it loses. It never changes what is shown or paged, and a person still promotes. Shown in `/monitoring` (`challenger_shadow`), as `oncall_challenger_*` gauges, in the job meta and in each version's card.
- `src/drift.py`: input drift per job, between the alerts a model learns from and the alerts it scores. PSI on severity, source, alert type and message length, a KS test on alerts per hour, and a classifier two-sample test on message text (a logistic regression that tries to tell the windows apart; its AUC and the terms that moved are reported). The result is stored in each job, shown in `/monitoring` (`input_drift`), exported as `oncall_input_drift_*` gauges, and has a Prometheus alert rule. It reports only and never changes a decision.
- `run_baseline_compare.py` and `src/baselines_anomaly.py`: z-score, EWMA and isolation-forest baselines through the same causal queue, with 95% bootstrap intervals over the 1,223 GAIA test faults and a paired difference to the rate rule. Re-run from the raw GAIA files, it reproduces 72.6% and 68.9% exactly. At K = 50 the detector and the rate rule are tied.
- `run_rcaeval_gate_split.py`: RCAEval RE2/RE3 with train, validation and test cases kept apart, so the promotion gate decides on cases that are never scored. It refuses the classifier on all six applications; the earlier "promoted on RE2 Train Ticket" was decided on its own test cases.
- `report/`: the four-page final report (LaTeX source and PDF) for the course brief.
- `run_reproduce.py`: one command that reproduces the GAIA result on a committed real 10-day sample (`app/examples/gaia_sample/`, 1.7 MB) in about 10 seconds, or the full tables with `--full`. `tools/make_gaia_sample.py` builds the sample and `tools/check_fresh_clone.py` checks a clean clone.
- A Windows job in CI.
- `README.md`: team and contributions, tested environment, and sections in the brief's order.

### Moved

- The trace-model experiment (`trace_eval`, `trace_gnn`, `trace_graph`, `trace_model`, `run_trace_eval.py`, `train_trace_model.py`) and the metric-model/ranker ablation (`model_eval`, `run_model_ablation.py`) moved from `src/` and `app/` to `app/experiments/traces/` and `app/experiments/model_ablation/`. Nothing in `product/` used them. Run them with `python -m experiments.traces.run_trace_eval` and `python -m experiments.model_ablation.run_model_ablation`.
- `product/pipeline.py` (853 lines) lost its promotion gate (`promotion.py`), the held-out queue check (`queue_check.py`) and the split and fit summaries (`training_report.py`); it is now about 630 lines. Behaviour is unchanged and `product.pipeline` still exports `promotion_decision` and `promotion_margin`.

### Fixed

- `run_trace_eval.py` imported the POSIX-only `resource` module at load time, which failed five tests on Windows.

### Changed

- The viva, demo and speaker notes moved to `presentation/`, the proposal drafts to `docs/history/`, and the generated Word and PDF copies are no longer tracked.
- `FINAL_REPORT.md`: 278 checked numbers (was 195), the independent gate test, and corrected test counts (475 tests: 465 pass, 10 skip).

## [1.1.0] - 2026-10-21

Optional models from PRs #25, #26 and #27 (merged at `d329532`). Every new flag stays off. Live paging is unchanged: `APP_PASSWORD`, a channel, the typed word `LIVE`, and an ingest job. LLM notes are written after the page decision, onto the shadow record only. They do not page and they do not reorder the queue.

### Decisions

These are the measured results already written in `docs/ML_MODELS.md`. This release did not rescore them and does not add counts that file does not contain.

- **Learned ranker: kept.** The PR #26 GAIA ranker stays a keep. The synthetic ranker dropped (2 caught against a baseline of 2, interval 0.0 / 0.0 / 0.0) because each day already fit in K. No GAIA catch count for the ranker is written in that document, so none is stated here. `LEARNED_RANKER` stays off.
- **Contrastive log encoder: kept.** On GAIA, seed 42 moved the published queue from 851 to 860 of 1223 faults, and 5 of 6 seeds kept. The synthetic encoder dropped (4 of 5 either way, interval 0.0 / 0.0 / 0.0, false items a day 0.71 to 1.0). `LOG_ENCODER` stays unset. `LLM_TEMPLATE_FEATURES` stays off, so the default queue does not use the encoder.
- **Trace model: kept only on pooled RCAEval RE2+RE3.** Commit `fda695c`, seeds 42, 0, 1, 2, 3. The seed rule keeps `trace_model` on 4 of 5 seeds and `trace_residual` on 4 of 5. Pooled versus the trace baseline, the mean per-fault catch difference is 0.0453 (interval 0.0142 to 0.0783) for the model and 0.0651 (interval 0.0283 to 0.1038) for the residual. Pooled K = 20 catches, in that seed order: model 140, 157, 147, 148, 146; residual 151, 155, 158, 144, 151. The queue caught 94 and the baseline caught 138 of 212 faults. Each suite drops on every seed. On RE2 the model caught 113 to 119 against a baseline of 121. On RE3 the model stayed below the queue's 39. The pool has 28 training cases (7 per system). `TRACE_MODEL` stays off. The model does not enter the queue and does not page. Earlier single-seed runs are not the shipped decision: `5860767` dropped (pooled K = 20: queue 94/212, baseline 138, model 143, residual 143, versus baseline +0.0236 with an interval through 0), and the `b1ba8b9` seed-42 catch of 147 is CUDA noise, not a keep.
- **Metric TCN: dropped.** The published GAIA extract is one memory channel per service. The RTX 5090 run saw the channel list `["memory"]`. A one-channel forecast is not the joint shape this model is for, so there is no GAIA keep. The synthetic keep (5 of 5 faults against 2, interval 0.6 / 0.2 / 1.0) does not turn the flag on. `METRIC_MODEL` stays off.
- **LoRA: adds nothing over the template.** On `b1ba8b9`, `--generate --limit 60`: exact template match 60/60, template rubric 60/60, grounding 0/60 for the base and the adapter. The template has no why clause, and the adapter copies the template, so a grounding zero is what that target produces. `injection_echo` is 0/60. The older 40/60 `instruction_following` count was loose words (`reply`, `previous`, `ignore`), not an injected instruction. `LLM_LORA` stays off. A note cannot page.

### Changed

- Version 1.1.0: `app/pyproject.toml`, `app/uv.lock`, `product.__version__` (the API version and the model-card fallback), the doc headers, `docs/ML_MODELS.md`, `docs/GPU_LLM_ROLES.md`, the report header and date (21 October 2026), and `submission/README.md`. The Word and PDF copies are not regenerated in this commit. The current suite figure is 450 passed, 10 skipped, line coverage 86% (recorded under 1.2.0). `run_validate.py` still matches 195 checked numbers.

### Fixed
- `certifi` 2026.7.22 to 2026.10.19 (`PYSEC-2026-4320` / GHSA-6q48-6c7w-9gw2). The 2026.7.22 bundle still trusted emSign roots whose intermediates were revoked after a key compromise. The pin was already on `master`; the 1.1.0 `uv.lock` edit had only changed the package version string.
- The CUDA determinism test accepts either PyG status: `pyg-unavailable` when torch-geometric does not import, and `pyg-best-effort` when it does and the torch module does not report deterministic algorithms. On the RTX 5090 run of `fda695c`, seeds 42, 0, 1, 2, 3, repeating seed 42 was identical. The seed rule keeps `trace_model` on 4 of 5 seeds (pooled versus baseline mean 0.0453, interval 0.0142 to 0.0783) and `trace_residual` on 4 of 5 (0.0651, interval 0.0283 to 0.1038). Pooled K = 20 catches were model 140, 157, 147, 148, 146 and residual 151, 155, 158, 144, 151, against queue 94 and baseline 138 of 212 faults. Each suite drops on every seed (RE2 model 113 to 119 versus baseline 121; RE3 below the queue's 39). The keep is only the pooled RE2+RE3 comparison, 28 training cases, a small sample. `TRACE_MODEL` stays off. `app/results/experiments.jsonl` stays append-only; replacing it with an older copy drops run rows.
- Trace GNN training sets the same CUDA determinism switches as the metric TCN, plus `CUBLAS_WORKSPACE_CONFIG=:4096:8`, TF32 off, a CPU epoch order, and PyG's deterministic scatter when that build has one. `run_trace_eval.py --seeds` reports every seed. A variant is kept only when more than half of the scored seeds keep and the mean per-fault catch difference across those seeds stays above 0 against both the queue and the trace baseline. On the RTX 5090, seed 42 caught 143 and dropped at `5860767` and caught 147 and would have kept at `b1ba8b9` (versus baseline +0.0425 [0.0047, 0.0802]); the residual moved 143 to 144 and the RE3 model 15 to 12. That flip is CUDA noise. The 147 is not a keep. LoRA training is seeded before the adapter is initialised; a different `adapter.safetensors` hash on CUDA is still expected. Grounding and `injection_echo` now use the same instruction phrases. A loose word such as `reply` is not instruction-following. The `b1ba8b9` grounding-plus-injected-log run flagged 40/60 notes that way while `injection_echo` was 0/60. Grounding stays 0/60 because the template has no why clause. Pooled false items per day at K = 20 on that run is 2.84. Flags stay off.
- The grounding rubric only treats identifier tokens (snake_case, dotted or hyphenated ids, numbers with a unit) as names, and it checks those against the alert's own fields. Ordinary words are not invented names. Log text is stripped of instruction phrases before it enters the prompt, and a note that echoes an injected instruction is replaced by the template note. That note still does not page. `--generate` records the echo rate under `injection_echo`. Held-out LoRA loss reports the skeleton sample and a random sample that honours `--held-out-limit`; the default batch is 2. A trace slice with fewer than four root-cause windows is written as skipped, and pooled false items per day is the sum of false items divided by the sum of suite days. On the RTX 5090 run of 5860767 the trace model and the residual both dropped on every RCAEval slice (pooled K = 20: queue 94/212, baseline 138, model 143, residual 143, versus baseline +0.0236 with an interval through 0; 7 training cases per system) and do not beat the jump baseline. LoRA matched the template 60/60 and added nothing over it (37 training skeletons). The contrastive encoder stayed a keep on GAIA (851 to 860, 5 of 6 seeds) and the ranker in PR #26 stayed a keep. Flags stay off.
- A combined RE2+RE3 trace run no longer feeds `detect_jumps` two values at one `(series_id, timestamp)`. RE3 is shifted by 1000 days; Online Boutique and Train Ticket still share a day inside a suite. The trace JSON reports per-suite, per-system, and pooled scores, with `train_cases`, `test_cases`, and `tiny_train` (fewer than 20 training cases). `trace_residual` is a separate fit that adds the baseline jump score as a node feature. The keep rule is unchanged. LoRA held-out loss is the mean over supervised tokens on one row per note skeleton, capped by `--held-out-limit` (default 500), and it prints progress. The training JSON records how repetitive the template targets are. The rubric also reports exact and near matches to that template, and a grounding score that asks for a reason and a check instead of the template wording.
- RCAEval trace eval no longer concatenates every span and rebuilds a graph per fault. Each case is read on its own, with only the columns the graph needs, and that graph is reused for fit, the baseline, and prediction. Rates are a groupby and edges are a merge on span id. `--suite` and `--max-cases` limit the run, and each case logs progress. LoRA training builds the decision-time rows in one pass; held-out is every later day. `run_llm_lora_rubric.py --generate` defaults to 60 held-out alerts and writes why each row failed.
- RCAEval span times prefer `startTimeMillis` / `startTime`. A text `time` column such as `21:24` is not a span time (`src/trace_graph.py` raised `TypeError` on that StringDtype). `run_trace_eval.py --data rcaeval` exits before scoring when `cases.parquet` is missing and names `tools/fetch_rcaeval_re23.py`. LoRA training and generation share `format_note_chat` (the chat template, with an end-of-text token after the target note). The base and the adapter stay loaded. `train_llm_lora.py` prints training loss and held-out loss. `run_llm_lora_rubric.py` stays template-only unless `--generate`, which scores the base and the adapter on held-out GAIA alerts. `run_log_encoder_ablation.py` scores the published full queue (metric items and log alerts, window ending at the last alert) and `--seeds` reports every seed.
- The optional-model ablation now uses the published queue window: the first alert through one second after the last alert. The GAIA run that stopped at the last metric point (849/1221, false items a day 1.8) is not comparable to the published 851/1223 row and is not repeated here. The ranker applies its score gate only on days with more slot-taking candidates than K. Walk-forward output includes `pooled`, one per-fault bootstrap across folds. GPU install instructions use `uv sync --inexact` and `uv run --no-sync`, because a plain `uv sync` removes cuML, torch and sentence-transformers. CUDA TCN weights are not bit-reproducible; seed flags are set and the limitation is documented in `docs/ML_MODELS.md`.
- With template features on, a classifier that is not given an encoder reads `LLM_TEMPLATE_ENCODER`. It was building `hash-v1` whenever `template_encoder_name` was unset, so the ablation and the production flag ignored a pinned sentence model while `/health` could already show that model. `/health` now reports the requested path before the first load. Sentence embeddings are cached per normalised template. A saved `hash-v1` model still stays on the hash.
- XGBoost 3.4 reports the device as `cuda:0`. The GPU fit accepts `cuda` and `cuda:N`. Any other device still falls back to sklearn, so a silent CPU fit is not labelled `xgboost-gpu`.
- `ACCEL_BACKEND=auto` (the default) stays sklearn even when cuML is installed. On an RTX 5090 Laptop, three same-seed cuML fits differed by up to 0.0375 in probability, so cuML is opt-in (`ACCEL_BACKEND=cuml`). Explicit cuML on GAIA alerts still agrees with sklearn: mean |ΔP| 0.000410, Jaccard 0.9856.
- A pinned sentence-transformer weight file is hashed, and the model loads from that file's folder. A failed load is logged and recorded (`encoder_load_status`, and `template_encoder` on `/health`) instead of only falling back to `hash-v1`.
- `Dockerfile.gpu` uses `docker.io/nvidia/cuda:12.6.3-runtime-ubuntu24.04` so a podman build does not depend on an unqualified-search registry.

### Added
- Optional trace root-cause model, LoRA shadow notes, and a contrastive log-template encoder, all off (`TRACE_MODEL`, `LLM_LORA`, `LOG_ENCODER=contrastive`). The trace model scores one service per causal span window (PyTorch Geometric when it imports, otherwise one plain-torch message-passing layer) and is kept only when the K = 20 catch interval beats both the jump baseline and the queue. A catch counts only on the root-cause service. It does not enter the queue. LoRA trains on the decision-time prompt `explain()` already builds, paired with a template note, and loads only as a shadow note with fallback to the GGUF and then to no note. The contrastive encoder is an InfoNCE alternative to hash-v1 / MiniLM; about 82% of GAIA faults have no log line, and the 4-number projection still caps hash-v1 and MiniLM (`LOG_ENCODER_DIM` is for this encoder only). Weights are safetensors plus JSON with a SHA-256 sidecar. Pickle and zip are refused. Neither path pages. On the 14-day synthetic sample (torch 2.14.1+cu130, no CUDA device, seed 42) the trace model caught 8 of 8 held-out faults against 4 for the trace baseline and 2 for the queue (intervals 0.5 / 0.125 / 0.875 and 0.75 / 0.375 / 1.0; baseline against the queue was 0.25 / 0.0 / 0.625 and dropped); the encoder caught 4 of 5 either way (interval 0.0 / 0.0 / 0.0) and dropped, with false items a day 0.71 to 1.0; the LoRA rubric passed 945 of 945 template notes and failed a note that invented 99999 and repeated PAGED-NOW. Flags stay off. The RTX 5090 measurements and commands are in the Fixed note above and in `docs/ML_MODELS.md`.
- Optional multivariate metric model and learned ranker, both off (`METRIC_MODEL`, `LEARNED_RANKER`). The metric model is a small causal TCN: it forecasts the next bin of a service's channels together and proposes a rule item from the forecast error. The ranker scores the day's candidates (XGBoost `rank:ndcg`, a tiny torch pairwise net, or a pointwise logistic model) and skips a low score so a later item can take the slot when the day has more candidates than K. Train on the first week only. Weights are safetensors or JSON with a SHA-256 sidecar; pickle and zip are refused. Neither model pages. CPU CI does not install torch. On the published GAIA extract the TCN sees only the memory channel, so it is dropped for 1.1.0. `METRIC_MODEL` stays off. The ranker keep does not set `LEARNED_RANKER`. Design and the RTX 5090 commands are in `docs/ML_MODELS.md`.
- Optional CUDA image `app/Dockerfile.gpu` and a Compose profile `gpu` (`oncall-queue-gpu`). `docker compose up` and CI still build `app/Dockerfile` (CPU). `/health` adds `accel.backend` and `llm` (`enabled`, `weights` of `missing` / `present` / `hash-ok` / `hash-mismatch`, `last_error`) and still does not probe Prometheus or hash weights while explanations are off. `/metrics` records shadow-note latency (`oncall_llm_explain_duration_seconds`), timeout rate, fallback rate, and GPU memory when `nvidia-smi` answers. Template-feature drift on `/monitoring` stays `n/a` while `LLM_TEMPLATE_FEATURES` is off.
- Optional template-hash features for the alert classifier (`LLM_TEMPLATE_FEATURES`, off by default). Five columns: a 4-number hash of the current log template and a count of earlier copies. A sentence-transformer weight file is used only when `LLM_TEMPLATE_ENCODER` matches `LLM_TEMPLATE_ENCODER_SHA256`; the model loads from that file's folder, otherwise the hash encoder is used and the failure is recorded. On the synthetic 14-day sample (seed 42, K = 20, first week / later days) both queues caught 4 of 5 held-out faults (detection 0.8); the per-fault catch difference had a 95% interval of 0.0 to 0.0, and false items a day went from 0.71 to 1.0. On GAIA (K = 20, 3630/31233 alerts, 1223 faults) `hash-v1` caught 843 vs 851 (68.93% vs 69.58%, false items a day 2.07 vs 1.82, 95% interval −0.0139 / −0.0065 / 0.0008) and all-MiniLM-L6-v2 caught 856 vs 851 (69.99%, 1.69 false items a day, interval −0.0008 / 0.0041 / 0.0098). Both drop. The four-number projection caps the encoder. The flag stays off. The default queue is unchanged.
- RTX 5090 Laptop timings (CUDA 13.2, cuml-cu13 26.10.0, xgboost 3.4.1): GAIA cuML single fit 1.17× and walk-forward 0.99×; xgboost-gpu 0.97× and 0.67×. No meaningful training speedup. Tables in `app/perf/ACCEL_BENCH.md`.
- Shadow-only explanations, off unless `LLM_EXPLAIN` is set. A local GGUF may write a short note beside `would_have_paged`. The note uses decision-time fields only. Missing weights, a hash mismatch, or a timeout leave the same queue with no notes, and the note cannot page or reorder items. The model is pinned by name, revision, and SHA-256 in `app/product/llm_pin.json`. `app/scripts/fetch_llm.py` downloads it and checks the hash. Weights (`*.gguf`, `*.safetensors`) are not committed. Released in 1.1.0. `LLM_EXPLAIN` stays off. A note cannot page.

## [1.0.5]

### Changed
- Report: on RCAEval the promotion gate is run on the same later half of cases that is scored (`product_gate()` in `app/run_rcaeval_logs.py` splits at the ablation's case boundary), so "promoted on RE2 Train Ticket" was decided on its own test set, and the +13.3 points were measured on the same cases. This is now stated in §5, §9, §12, §13 and in the §B5 method and caveats, and listed under future work. The evaluation itself is unchanged (a further split of 45 cases would leave about 22 on each side), so no result number changed. `run_validate.py` now fails if the Train Ticket gate window stops being the scored set. In the product the gate window is the tail of a batch and later batches are scored, so the live design does not overlap.
- Stale numbers in shipped documents: `DEMO_SCRIPT.md`, `VIVA_ANSWERS.md`, `VALIDATION.md`, `design_specification.md`, `project_plan.md` and the speaker notes led with 69.6% as "the full queue". They now give the shipped configuration on GAIA (log alerts by severity: 71.0%, 1.4 false items a day, precision 92%) and name 69.6% as the queue with the classifier's alerts, which the gate refuses. The speaker notes also had 70 / 70.2 / 72.5 / 74.4 and "1.3 points"; they now match the report (71.0, 71.3, 72.6, 2.1). `docs/mlops.md` and `docs/test_plan.md` said 295 tests; they say 300 (+1 integration) and the coverage table was re-measured (89.83%).
- Version 1.0.5: `app/pyproject.toml`, `uv.lock`, `product.__version__`, the doc headers (including `docs/requirements.md`, which still said 1.0.0), the report header and date, `submission/README.md`. The report's Word and PDF copies are regenerated.

### Fixed
- `test_old_evaluations_are_pruned` depended on the file system giving three folders made 2 ms apart different modification times; on Linux it could fail (it did in a full run on one machine). It now sets the times explicitly. No product code changed.

### Fixed (already on `master` since 1.0.4: 14-15 October, PRs #21, #22, #23)
- Docker: **Run sample** works in the container (`app/Dockerfile` creates a writable `/app/data/sample_product`).
- `DEMO_SCRIPT.md` numbers and commands refreshed; `USE_YOUR_DATA.md` states that no pilot with a real organisation has been run; README runtime and data-fetch steps.
- `app/run_nab.py`: platform-independent NAB order. The first version had a syntax error that failed lint on `master` for about 13 minutes before PR #22 fixed it.
- ruff 0.16.10 to 0.17.0 (Dependabot, PR #23).

### Release process
- `v1.0.3` was moved after it was published; no tag is moved for this release.

## [1.0.4]

### Changed
- Report: the RCAEval RE2 Train Ticket gain is stated against both baselines: +13.3 points over no log alerts and +2.2 over severity alerts (one case) at K = 10 (§5, §12, §13). `run_validate.py` checks both (`rcalogs.RE2.tt.gain_vs_none`, `rcalogs.RE2.tt.gain_vs_severity`; 195 numbers).
- Report §9: the gate margin was chosen after the RCAEval gate results were seen, so those decisions are not an independent test of it; on Train Ticket the saving of 10 comes from 10 fewer false items, not a saved missed fault, although the setting is named `PROMOTION_MIN_SAVING_MISSES`.
- Version 1.0.4 everywhere: `app/pyproject.toml` and `uv.lock` (were 1.0.0), the API version and the model-card fallback now read `product.__version__` (were hard-coded 1.0.0), the report header, and the doc headers in `docs/` (were 1.0.0). New test `test_the_package_version_matches_pyproject` (301 tests).
- The report's Word and PDF copies are regenerated.

## [1.0.3]

### Changed
- **Promotion gate margin.** A newly trained model is promoted only if the held-out queue with its alerts costs at least one missed fault (`PROMOTION_MIN_SAVING_MISSES` × miss cost, default 1 × 10) less than both the queue without log alerts and the queue that runs if it is refused. One held-out case moves the cost by that much, so a smaller saving could come from that case alone. `0` restores the previous rule. On RCAEval the gate now promotes the classifier on RE2 Train Ticket only (saving 10, exactly the margin); RE2 Online Boutique (saving 3: 283 against 286 and 291) is now refused. GAIA is unchanged (refused). Tests in `test_model_registry.py` and `test_settings.py`; `run_validate.py` checks the margin, the savings and which applications promote.
- GAIA job timing: the report and docs gave both 27 s and 35.7 s. Re-measured three times on the same laptop with `perf/resources.py` (53.7, 54.9, 55.7 s; on battery power); every document now gives the median, 54.9 s, and the 27 s manual timing, which had no recorded machine, is dropped. Peak memory 505 MB.

### Fixed
- `docs/deployment.md` said login sessions were in-process (the scaling-path bullet), contradicting §3a (`auth.db` since 1.0.2). The in-process job, evaluation and `ops.json` locks are what keep the app to one worker.

### Added
- Report §11 and `docs/privacy_responsible_ai.md`: how to find and erase one person's ID in `queue.db`, `auth.db`, the audit log, evaluation uploads, model files and backups (a data-subject request). Report §7 and §8 now describe the `auth.db` sessions and the live-Prometheus fix from 1.0.2.

## [1.0.2] — 2026-10-11

### Changed (release)
- Docs and report: 296 tests (one in the new integration job); the report's Word and PDF copies are regenerated.


### Fixed
- **Live Prometheus data never confirmed a jump.** The ingest job fetched 14 days in one query; to stay under Prometheus' point limit that query used a 121 s step, and the jump rule links hot points at most `memory_gap_minutes` (2) apart, so no jump could be confirmed. The fetch now reads the window at 60 s in several non-overlapping queries. Found by the new integration test; the unit tests fed the detector data directly. Test: `test_a_14_day_fetch_keeps_one_minute_points_so_live_jumps_are_confirmed`.

### Added
- Integration test against real services: `app/integration/docker-compose.yml` runs the app beside Prometheus and Alertmanager, and `tests/integration/test_compose.py` checks webhook → ingest job with Prometheus series → nothing paged in shadow mode. CI runs it in a new `integration` job.
- Sign-in sessions, the login lock and the ingest rate counters are kept in `auth.db` (SQLite, beside `ops.json`; `AUTH_DB` moves it) instead of process memory. A restart no longer signs everyone out or clears a lock earned by guessing passwords. Session tokens are stored as SHA-256. Tests in `test_auth_store.py`, two of which start the app twice in separate processes.
- `.github/workflows/dependabot-lock.yml`: on a Dependabot PR that changes `app/uv.lock`, regenerate `app/requirements.lock` on its branch and rerun CI. Dependabot's `uv` updater leaves `requirements.lock` stale, so every runtime-dependency bump failed CI's lock check (PR 12). ADR 0007 records it.

### Changed
- fastapi 0.143.0 (adds opentelemetry-api 1.45.1), mypy 2.4.0, ruff 0.16.10, pyarrow 25.0.1 (dev). Dependabot uses the `uv` ecosystem.

## [1.0.1] — 2026-10-11

### Changed (release)
- Docs and report: 289 tests, the coverage table (89.8%), and the model-retention row for `MODEL_CANDIDATE_KEEP`; the report's Word and PDF copies are regenerated.

### Fixed (code review, 11 October 2026, fourth round)
- Paging keys: `PAGE_KEY_MAX` dropped keys in alphabetical order (all `gap:` keys before any `jump:` key), so once the list was full an item still in the history lost its key and was paged again on every job. Keys of items still shown are now never dropped; the rest go oldest first by start time. Test: `test_an_item_still_in_history_is_never_paged_twice_once_the_key_cap_is_reached`.
- Backup copied the live WAL-mode `queue.db` as a plain file, which could tear during a job and missed commits still in the write-ahead log. It now uses SQLite's backup API, and restore deletes the lost database's `-wal` and `-shm` so they are not replayed. Tests in `test_backup.py`.
- `LOG_FORMAT=json` dropped tracebacks; the JSON line now has `exc` and `stack`. Test: `test_logging_config.py`.
- `recent_jobs`, `latest_job_id` and job retention ordered by `created_at` only, so two jobs saved in the same clock tick came back in either order and `/monitoring` could read an older job as the last one. Ties now break on insertion order. Test: `test_jobs_saved_in_the_same_clock_tick_keep_their_order`.
- The release workflow published an image without running any check. It now runs every CI job (`ci.yml` as a reusable workflow) on the tagged commit first.

### Changed (code review, 11 October 2026, fourth round)
- Metric drift on `/monitoring` is per series: the median of each series' last hour against its own earlier jobs, with the drifted series listed (`data.metric_drift_series`). The fleet-wide median over the whole re-scored history barely moved and hid a single service. Jobs saved before this fall back to the old check. Tests in `test_monitoring.py`.
- Refused candidate models: only the newest `MODEL_CANDIDATE_KEEP` (default 20) keep their file; the rest are deleted and their cards marked `file_removed`. Versions that were ever active are never deleted. Test: `test_only_the_newest_refused_models_keep_their_files`.

## [1.0.0] — 2026-10-11

### Added (code review, 11 October 2026, third round)
- Second dataset for the alert classifier: `tools/fetch_rcaeval_re23.py` downloads RCAEval RE2 and RE3 logs and metrics (720 files, about 570 MB, SHA-256 of every file pinned in `tools/rcaeval_re23_sha256.txt`; git-ignored), and `run_rcaeval_logs.py` runs the classifier / severity / no-log-alerts ablation and the product's promotion gate on each application (`results/rcaeval_logs.csv`, `.json`; report §B5). On RE2 Train Ticket the classifier adds 13.3 points (95% interval +4.4 to +24.4) and the gate promotes it there and on RE2 Online Boutique; elsewhere it adds at most one case and the gate keeps it out. Tests in `test_rcaeval_logs.py`.
- `perf/resources.py` measures one GAIA scoring job (35.7 s, 500 MB peak, about 215 KB of database a day); report §7 has an operating-cost estimate with its assumptions and cited prices.
- UML use-case diagram (`figures/use_cases.svg` and `.png`, drawn by `make_figures.py`), a data-retention table (§11) and numeric retrain and rollback triggers (§9).
- Report §B1: the shipped configuration (the gate keeps the classifier out on GAIA) is its own row, 71.0% at K = 20. `run_validate.py` now checks 189 numbers; 279 tests.

### Changed (code review, 11 October 2026, third round)
- The promotion gate's queue check compares the new model with what the batch falls back to if the model is refused (the active model, or severity rules), as well as with no log alerts (`_queue_check(..., fallback=...)`). On GAIA the fallback queue catches 871 of 1,223 held-out faults with 70 false items; the classifier still fails the gate.
- Two experiment-log notes said "Instructor review"; they now say "Code review".

### Changed (code review, 10 October 2026, second round)
- The alert classifier is no longer on by default. The promotion gate (`promotion_decision`) now also requires a new model to beat showing **no log alerts**, alert by alert and in the **whole held-out queue** (10 × missed faults + false items), and the first model is no longer promoted just because none is active. A model that fails stays a candidate and does not score the batch it was trained on (rules only or the active model does). On GAIA the classifier fails the queue test (848 vs 872 of 1,223 held-out faults caught), so GAIA runs on rules only. Tests in `test_model_registry.py`.
- `run_classifier_ablation.py` also writes `results/classifier_ablation_gaia.json`: why the severity row attaches no alerts, and the gate's decision on GAIA. `run_validate.py` checks 18 more numbers (93 in all) and that the gate still keeps the classifier out on GAIA.
- Report header fields on separate lines; report dated 11 October; design specification dated 10 October; coverage table in `docs/test_plan.md` refreshed; the `save_model` comment says what a crash between the two `os.replace` calls can still do.

### Fixed (code review, 10 October 2026, second round)
- Every `X-Forwarded-For` and `Forwarded` header line is read, not only the first, so a proxy that adds its own line cannot be bypassed by the client's line. Regression tests `test_every_forwarded_header_line_is_read` and `test_login_lock_holds_when_the_proxy_adds_its_own_xff_line`. `docs/deployment.md`: `TRUSTED_PROXIES` lists only the proxy's own addresses, never a client subnet.

### Added (code review, 10 October 2026, second round)
- `app/tests/conftest.py`: every test starts with the sign-in, proxy, ingest-limit and connector variables unset, so a shell with `APP_PASSWORD` exported no longer turns the suite red. 273 tests.

### Fixed (code review, 10 October 2026)
- CI audit job: `pip-audit` has no `--severity` option, so the job failed on every push. It now runs `uvx pip-audit -r requirements.lock --require-hashes --disable-pip`.
- Login lockout bypass behind a trusted proxy: the client address was the **leftmost** `X-Forwarded-For` value, which the client controls. It is now the rightmost hop that is not in `TRUSTED_PROXIES`. Regression test `test_login_lock_cannot_be_split_by_prepending_xff`.
- `X-Forwarded-Proto` marks the session cookie Secure only when it comes from a trusted proxy.
- `save_model` writes the model and its SHA-256 to temp files and swaps both in with `os.replace` under a lock, so a crash or two concurrent saves cannot leave a model whose digest does not match.
- Docker CMD states `--workers 1`; `docs/deployment.md` documents `TRUSTED_PROXIES` vs `uvicorn --forwarded-allow-ips`.

### Added (code review, 10 October 2026)
- `tools/fetch_gaia.py`: downloads the GAIA MicroSS run tables and the 37 memory series used, without an external unzip tool, and checks each file's SHA-256.
- `run_classifier_ablation.py` and `results/classifier_ablation_gaia.csv`: the full queue with log alerts chosen by the classifier, by severity, or not at all. At K = 20: 69.6%, 71.0%, 71.2%. The classifier adds no catches on GAIA; the report now says so (§5, §12, §B4). `run_validate.py` checks the six new numbers (75 in all).
- Report §C1a, use of AI tools.

### Changed (code review, 10 October 2026)
- One title everywhere: *AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices*, with *Alert Fatigue Reduction in Cloud Microservice Operations* as the subtitle.
- Report date, test counts (267), checked-number counts, project plan D6/T13, and the README pointer to the submitted proposal PDF.
- Submitted proposal (PDF and its Word source), `queue_flowchart.png` and `package.json` / `package-lock.json` committed; course material and draft Word files kept out of git.


### Added (submission hardening)
- `TRUSTED_PROXIES`: login lock and ingest rate limits key on the forwarded client only when the socket is a listed proxy.
- Ingest batch cap (`INGEST_MAX_BATCHES` / `INGEST_WINDOW_SECONDS` → 429) and alert-count cap (`INGEST_MAX_ALERTS` → 413).
- `POST /models/reset` quarantines a corrupt `registry.json`; `GET /models` returns 503 until then.
- Metric-median drift on `/monitoring` (same 0.5–2 band as alert volume).
- Backup/restore (`python -m product.backup`), JSON logs (`LOG_FORMAT=json`), audit-log rotation, paging-key cap (`PAGE_KEY_MAX`).
- CI `pip-audit`, Dependabot, tag-triggered image publish, `.gitattributes` (LF).

### Added (Phase 3, from the course brief review)
- Model registry: every trained model kept as a version with a card; promotion gate on held-out cost; rollback API (`/models`).
- Experiment log `app/results/experiments.jsonl` written by every experiment script.
- Production monitoring: `/metrics` (Prometheus) and `/monitoring` (checked health).
- Load test `app/perf/load_test.py` and results; AI-specific input tests; per-environment settings `app/deploy/*.env`.
- Documents: `docs/deployment.md`, `docs/mlops.md`, `docs/monitoring.md`, `docs/privacy_responsible_ai.md`.

### Fixed (Phase 3)
- A webhook row without a usable timestamp or source was buffered and made every later scoring job fail; rows are now validated.

### Changed (Phase 2 refactor — no result changed; GAIA and NAB reruns are identical)
- `product/app.py` split into `product/web/` (security, schemas, uploads, context, three route modules).
- `product/pipeline.py` split into `inputs.py`, `labels.py`, `paging.py`; `run_job` is a short orchestrator.
- One definition of a catch and one threshold search in `src/scoring.py`, replacing four hand-written overlap checks and two copies of the tuning loop.
- `QueueItem.payload` is typed; mypy 39 errors → 0 (CI budget 0).
- Alerts-only milestone scripts moved to `app/experiments/legacy_alerts_only/`; deck builder to `tools/build_rehearsal.py`.
- Tests for `experiment.py`, `scoring.py` and `settings.py` (186 → 200 tests, coverage 86% → 89%).

### Added
- Requirements with IDs, user stories and a traceability matrix (`docs/requirements.md`).
- Architecture diagrams: context, components, ingest-to-page sequence, deployment, data model (`docs/architecture.md`).
- Decision records (`docs/adr/`), threat model (`docs/threat_model.md`), test plan (`docs/test_plan.md`), retrospective (`docs/retrospective.md`).
- `app/pyproject.toml`, `app/uv.lock` and a hash-pinned `app/requirements.lock`; the Docker image installs the lock.
- CI: lock check, ruff, a mypy error budget, tests on Python 3.10 and 3.12 with coverage ≥ 85%, `run_validate.py`, Docker build and health check.
- `FINAL_REPORT.md` *Checked numbers* table; `run_validate.py` reads it instead of numbers typed into its own source.
- Audit log for changes to Live mode, keys, the Prometheus URL and settings.
- Ingest de-duplication (`INGEST_DEDUP_HOURS`), `ALLOWED_HOSTS`, `MAX_INGEST_MB`.
- Tests: `test_security.py`, `test_reliability.py`, `test_causality.py`, `test_run_validate.py` (150 → 186 tests).

### Changed
- The queue has no look-ahead left: alert filters are first come, first served; a leak is dropped only by a jump already shown; the robust spread uses earlier points only; an alert rides on a jump only while `Spike.open_at` says it is open; a dead feed is reported before data returns.
- Results after that change: full queue at K = 20 70.2% → 69.6%; walk-forward 72.5% → 71.3%, now tied with the rate rule week by week; the detector alone unchanged at 72.6% (8/8 weeks). SMD at K = 20: 55.1% of incidents.
- Live paging needs `APP_PASSWORD`, checked on the switch and in `can_page()`.
- Docker and compose default to `PROM_ALLOW_PRIVATE=0`.
- Page marks are kept per channel, so a retry re-sends only to the channel that failed.

### Fixed
- SSRF: numeric and dotted forms of loopback, and public names that resolve to private or metadata addresses, passed the Prometheus check; redirects were followed.
- A request through a reverse proxy was treated as private and served without a password.
- DNS rebinding and cross-site requests could drive the local API.
- The ingest token was accepted in the URL.
- Large uploads reached temp disk before the size check; `/ingest/*` had no body limit.
- SQLite connections were never closed; concurrent `ops.json` writes could lose a change.
- A retry after 409 buffered, and could page, the same alerts twice; `/evaluate` could run in parallel and left folders behind on errors.

## [0.4.0] — 2026-09-27 — Connectors and pilot hardening
### Added
- Live PagerDuty with a typed `LIVE` switch and demo guards; Slack and Opsgenie with the same item list; *would-have-paged* in shadow mode.
- Prometheus test pull from the Connectors page; per-series jump size; SMD benchmark; demo dry-run; UAE pilot offer.
- One job at a time (409), job and evaluation retention, upload cap, input checks, cheap `/health`, first SSRF gate, Docker image.
### Fixed
- Page only metric items new since the last ingest job; hide Prometheus URL credentials.
- A jump at the same instant as an alert keeps its slot; misses are scored against the full queue at the largest K.

## [0.3.0] — 2026-09-26 — Causal queue and fair baselines
### Added
- Causal, first-come daily queue; gap items; slow-leak rule; Prometheus-style rate and static baselines; oracle ceiling.
- *Evaluate my data* in the app and CLI for any metric table; per-series confirm levels; demo script.
### Changed
- Hindsight ranking by final peak replaced by a confirmation threshold learned on week 1.
- Superseded experiment scripts removed; every document refreshed after the first code review (`CODE_REVIEW.md`).

## [0.2.0] — 2026-09-24 — Honest statistics
### Changed
- The 95% detection target replaced by walk-forward statistics with a sign test and bootstrap interval.
### Fixed
- Fold-7 explanation (a monitoring outage) and the sign-test p-value.
### Added
- Prometheus reachability on the Connectors dashboard.

## [0.1.0] — 2026-09-23 — First version
### Added
- GAIA loader with China-to-UTC clock alignment, alert classifier, memory-jump detector, jump-first policy, local on-call web app.
