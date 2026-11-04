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
  # The app answers 400 "Ingest buffer is empty" when no new alert arrived since
  # the last job (Alertmanager re-sends are de-duplicated). That is a normal
  # quiet window, not a failure.
  response="${DATA}/score-response.json"
  code="$(curl -sS -o "${response}" -w '%{http_code}' -X POST "${APP_URL}/jobs/run" \
    -H 'Content-Type: application/json' \
    -d "${body}")" || true
  code="${code:-000}"
  stamp="$(date -u +%FT%TZ)"
  if [[ "${code}" == "200" ]]; then
    printf '%s accepted ' "${stamp}"
    cat "${response}"
    printf '\n'
  elif [[ "${code}" == "400" ]] && grep -q 'Ingest buffer is empty' "${response}" 2>/dev/null; then
    echo "${stamp} score loop: no new alerts since the last job"
  else
    echo "${stamp} score loop: the app did not accept the shadow job (HTTP ${code}): $(head -c 300 "${response}" 2>/dev/null)" >&2
  fi
  if [[ "${once}" == "1" ]]; then
    exit 0
  fi
  sleep 600
done
