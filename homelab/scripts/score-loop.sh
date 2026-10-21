#!/usr/bin/env bash
# Ask the in-cluster app to score its ingest buffer. The body is shadow. Always.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

once=0
if [[ "${1:-}" == "--once" ]]; then
  once=1
fi

body='{"mode":"ingest","budget_k":20,"shadow_mode":true}'
while true; do
  if [[ ! -f "${DATA}/endpoints.env" ]]; then
    echo "score loop: ${DATA}/endpoints.env is missing" >&2
    if [[ "${once}" == "1" ]]; then
      exit 1
    fi
    sleep 10
    continue
  fi
  # shellcheck disable=SC1091
  source "${DATA}/endpoints.env"
  if ! curl -fsS -X POST "${APP_URL}/jobs/run" \
    -H 'Content-Type: application/json' \
    -d "${body}"; then
    echo "score loop: the app did not accept the shadow job (it may still be buffering)" >&2
  fi
  printf '\n'
  if [[ "${once}" == "1" ]]; then
    exit 0
  fi
  sleep 600
done
