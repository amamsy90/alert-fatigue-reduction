# run from /home/ai/work/gaia-rerun-wt/app (clean worktree, detached at 1da70d94a6c4f2c8d14da4becddcb71c086f3b58)
# python: /home/ai/work/alert-fatigue-reduction/app/.venv/bin/python (existing GPU venv; uv.lock/pyproject unchanged since 0ff5eef)
/home/ai/work/alert-fatigue-reduction/app/.venv/bin/python run_model_ablation.py --data gaia --device cuda --backend xgboost --epochs 8 --out /home/ai/work/analysis/gaia-ranker-rerun-20261103T180033Z/model_ablation_gaia.json
/home/ai/work/alert-fatigue-reduction/app/.venv/bin/python run_model_ablation.py --data gaia --device cuda --backend xgboost --epochs 8 --walk-forward --out /home/ai/work/analysis/gaia-ranker-rerun-20261103T180033Z/model_ablation_gaia_walk.json
