# Homelab day-12 results

These files are the summaries and analysis scripts from the frozen snapshot
`day12-rebuilt-20261103T150116Z`, produced on ai-ROG. They are evidence. They
are not regenerated in CI, and they contain no raw alert or metric series.

The metric window starts at `2026-10-21T19:15:11Z` (`rebuild_summary.json`
`requested_start`; Prometheus's oldest sample in that file is
`2026-10-21T18:56:50Z`). `summary.json` records `train_end`
`2026-10-28T19:15:11Z` and `test_end` `2026-11-02T19:15:03Z`. The snapshot
cutoff used when the run was described is `2026-11-02T19:15:11Z`. The test
window in the bootstrap file has 6 days and 559 faults.

`rebuild_summary.json` says the alert file was rebuilt from Prometheus
(`requested_start` `2026-10-21T19:15:11Z`, `end` `2026-11-02T19:15:00Z`,
60-second step, 120-hour chunks, 120-second gap rule) into 1,184 episodes
across 44 series.

| Path | What it is |
|---|---|
| `rebuild_summary.json` | Rebuild of `alerts.csv` from Prometheus history |
| `plain/` | Day-12 evaluation without the learned ranker (`summary.json`, per-company and pooled queue tables, `leave_one_company_out.json`) |
| `ranker/` | The same run with the learned ranker scored (`summary.json`, queue tables, `leave_one_company_out.json`) |
| `pooled_bootstrap.csv`, `pooled_bootstrap.json`, `pooled_bootstrap.py` | Paired bootstrap of the ranker against `proposed`, `jumps_tuned` and `static_p99` |
| `feedback_bootstrap.csv`, `feedback_bootstrap.json`, `feedback_analysis.py` | Ranker against `logs_severity` and the Alertmanager-style replay |
| `topology_stats.json`, `topology_stats.py` | Alert mix, strict alignment, fold-down, queue rows used in §B7 |
| `fault_fanout.csv`, `fanout_chance.json`, `fanout_chance.py` | Loose 10-minute company window and the shifted-window chance line |
| `worked_example.json`, `worked_example.py` | One test-window alert and the jump that caught its fault |

The Python scripts take a snapshot directory and an output directory as
arguments. They do not embed a machine path. The JSON files still record the
directory used on ai-ROG, as the original run wrote it. Reproduce the tables
with `./homelab/scripts/reproduce_day12.sh`. The raw snapshot is
`homelab/snapshots/day12/`. `LEARNED_RANKER`, `METRIC_MODEL` and `LLM_EXPLAIN`
stay off in `summary.json` (`flags_changed` is false). The rebuild file has
1,184 episodes. `data/collected/alerts.csv` in the snapshot has 1,182, which
is what the day-12 evaluator scored after the metric-window cutoff.
