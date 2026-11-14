# Optional queue checks

Scored on the committed day-12 snapshot (`homelab/snapshots/day12/day12-rebuilt-20261103T150116Z.tar.xz`). The flags were set only in the scoring process. `LEARNED_RANKER`, `METRIC_MODEL` and `LLM_EXPLAIN` were unset. Nothing was paged.

| Check | Flag | What was scored |
|---|---|---|
| E1 Company calibration | `RANKER_COMPANY_CALIBRATION` | For each held-out company, N = 1, 2 and 3 days of its own labels, then the faults that start after that boundary. Threshold retune and sample-weighted refit (weight 4). |
| E2 Hybrid queue | `HYBRID_QUEUE` | Ranker fills M slots and proposed fills the rest. M is 10, 20 and min(K, 20). The learned hybrid's M is chosen on the training window. |
| E3 Topology features | `RANKER_TOPOLOGY_FEATURES` | Neighbour alerts in 15 minutes, an open jump on the same service, and fan-out from the public demo call graph. |
| E4 Time to detection | none (analysis only) | Minutes from fault start to the first catching item. Median, p90, and a bootstrap interval on the median difference versus proposed. |

KEEP means the 95% paired bootstrap interval on the per-fault catch difference has lower bound above 0 (4,000 draws, seed 42). A row with no calibration items is not scored and has no rate.

The home-lab tables are in `homelab-20261104T192524Z/summary.csv` and `detection.csv`. The corrected GAIA run is `gaia-20261104T194507Z/` (1,223 faults; at K = 20 proposed 851 and the learned ranker 887). `docs/report_appendices_full.md` Appendix I quotes both.

`gaia-20261104T193400Z-uncorrected/` is the ai-ROG run from before the window fix. Its fault count is not the published 1,223, so those rows are not results. Do not cite them.

On ai-ROG, from a worktree that has `app/data/gaia_raw` and `app/data/gaia_metrics`:

```bash
./experiments/run_enhancements_gaia.sh
```

The script installs the `gpu` dependency group and stops if `xgboost` cannot be imported. It writes `docs/results/enhancements/gaia-<utc>/`. Cite E2, E3 and E4 only after K = 20 shows 1,223 faults, proposed 851 and the learned ranker 887. E1 is the home-lab company split and is not part of that script.
