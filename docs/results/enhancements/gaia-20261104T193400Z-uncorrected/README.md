# Uncorrected GAIA run (do not cite)

ai-ROG run of `experiments/run_enhancements.py` at commit `c0b17f4`, saved here before the window fix. `commands.txt` records the GPU virtualenv. The first attempt used `run_enhancements_gaia.sh`, whose `uv run --directory app` environment had no xgboost and fell back to the pointwise model. That attempt was stopped. This folder is the GPU rerun.

The harness took its origin and stop from the home-lab span (earliest and latest of metrics, alerts and faults). The published GAIA split, in `run_model_ablation.py` / `published_bounds`, starts at the first alert and stops one second after the last alert. These files therefore do not match Table 1 (1,223 faults, proposed 851, ranker 887 at K = 20). Do not put the counts in this folder into the report.

The scorer now calls `published_scoring_window`. Re-run `experiments/run_enhancements_gaia.sh` and check that K = 20 prints `test_faults=1223`, proposed 851 and learned ranker 887 before citing E2, E3 or E4.
