# GAIA learned-ranker re-run (Nov 3 2026, ai-ROG RTX 5090)

Slim copy of `gaia-ranker-rerun-20261103T180033Z`. Logs and the experiments-jsonl diff are not included. Numbers in `FINAL_REPORT.md` §5 and §B7.2 come from `summary_by_k.csv` and `walk_forward_pooled.json`.

- Code: clean detached worktree /home/ai/work/gaia-rerun-wt at origin/master 1da70d9 (v1.1.0 course copies; includes PR #26). See git_sha.txt.
- Command: commands.sh (documented in docs/ML_MODELS.md "Train and score on GAIA"). Seed 42 (script default), bootstrap 4,000 draws, seed 42 (src/model_eval.py defaults). Keep rule unchanged.
- Python: existing GPU venv /home/ai/work/alert-fatigue-reduction/app/.venv used read-only (pyproject/uv.lock unchanged since 0ff5eef). env.txt.
- Data: app/data/gaia_raw + gaia_metrics copied from the main checkout (fingerprint in data_fingerprint.txt).
- Outputs: model_ablation_gaia.json (time split), model_ablation_gaia_walk.json (--walk-forward), summary_by_k.csv, run_plain.log, run_walk.log.
- "baseline" variant = the current causal queue; at K=20 it matches the `proposed` row of app/results/queue_eval_gaia.csv (851/1223, precision 0.895, 1.82 false/day).
- Every by_k entry (counts, caught_ids, CIs, decisions) is identical to the Oct 19 run in /home/ai/work/gpu2b/pr26b_results/model_ablation_gaia.json.
