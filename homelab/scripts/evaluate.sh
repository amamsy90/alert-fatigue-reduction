#!/usr/bin/env bash
# Score homelab/data with the repo's queue evaluator.
# Pass --learned-ranker to run leave-one-company-out. That flag does not set LEARNED_RANKER.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

extra=()
if [[ "${1:-}" == "--learned-ranker" ]]; then
  extra+=(--learned-ranker)
  shift
fi

py -m homelab.evaluate \
  --collected "${DATA}/collected" \
  --faults "${DATA}/faults.csv" \
  "${extra[@]}" \
  "$@"
