#!/usr/bin/env bash
# Show whether the lab is collecting: firing alerts, chaos experiments, ground truth.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

if command -v kubectl >/dev/null 2>&1; then
  log "workloads"
  kubectl get pods -A --no-headers 2>/dev/null | awk '$1 ~ /^(observability|chaos-mesh|company-)/ {print}' || true
  log "chaos experiments"
  kubectl get podchaos,networkchaos,stresschaos,httpchaos -A --no-headers 2>/dev/null || true
else
  log "kubectl is not installed"
fi

if [[ -f "${DATA}/endpoints.env" ]]; then
  # shellcheck disable=SC1091
  source "${DATA}/endpoints.env"
  if curl -fsS --get "${PROM_URL}/api/v1/query" \
    --data-urlencode 'query=count(ALERTS{alertstate="firing"})' \
    -o "${DATA}/firing.json"; then
    py -c 'import json,sys; data=json.load(open(sys.argv[1])); result=(data.get("data") or {}).get("result") or []; value=result[0]["value"][1] if result else "0"; print("firing alerts now:", value)' "${DATA}/firing.json"
  else
    log "prometheus port-forward is not answering on ${PROM_URL}"
  fi
  if curl -fsS "${APP_URL}/connectors" -o "${DATA}/connectors.json"; then
    py -m homelab.safety --connectors "${DATA}/connectors.json"
  else
    log "app port-forward is not answering on ${APP_URL}"
  fi
else
  log "no ${DATA}/endpoints.env yet; run scripts/up.sh"
fi

if [[ -f "${DATA}/collected/alerts.csv" ]]; then
  log "collected alert rows: $(($(wc -l < "${DATA}/collected/alerts.csv") - 1))"
else
  log "collected alert rows: 0"
fi
if [[ -f "${DATA}/faults.csv" ]]; then
  log "ground-truth rows: $(($(wc -l < "${DATA}/faults.csv") - 1))"
else
  log "ground-truth rows: 0"
fi
