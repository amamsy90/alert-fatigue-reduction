#!/usr/bin/env bash
# E2, E3 and E4 on GAIA. Run this on ai-ROG, from a worktree that already has the GAIA files.
# Worktrees on that machine live under /home/ai/work. uv is on PATH. xgboost can use CUDA.
#
# The three experiment flags are turned on for this process only.
# LEARNED_RANKER, METRIC_MODEL and LLM_EXPLAIN are not set, and this does not page.
# E1 is the homelab leave-one-company-out calibration. This script does not score it.
# Results go to docs/results/enhancements/gaia-<utc>/summary.csv and detection.csv.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${ROOT}/docs/results/enhancements/gaia-${STAMP}"
mkdir -p "${OUT}"
if [[ ! -d "${ROOT}/app/data/gaia_raw" || ! -d "${ROOT}/app/data/gaia_metrics" ]]; then
  echo "GAIA folders are not in this worktree (app/data/gaia_raw and app/data/gaia_metrics)." >&2
  echo "On ai-ROG, run this from a checkout under /home/ai/work that has those folders." >&2
  exit 2
fi
export RANKER_COMPANY_CALIBRATION=1
export HYBRID_QUEUE=1
export RANKER_TOPOLOGY_FEATURES=1
unset LEARNED_RANKER METRIC_MODEL LLM_EXPLAIN
# gpu is a dependency group, not a package extra. Without it, uv's app env has
# no xgboost and the ranker would silently fit the pointwise model.
if ! uv run --directory "${ROOT}/app" --group gpu python -c "import xgboost"; then
  echo "xgboost did not import. Refusing to score with --backend xgboost." >&2
  echo "Install the gpu group (uv run --directory app --group gpu) or use the GPU virtualenv." >&2
  echo "A missing import must not fall back to the pointwise model." >&2
  exit 1
fi
uv run --directory "${ROOT}/app" --group gpu python "${ROOT}/experiments/run_enhancements.py" \
  --data gaia \
  --backend xgboost \
  --device cuda \
  --out "${OUT}"
echo "wrote ${OUT}/summary.csv"
