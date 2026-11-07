#!/usr/bin/env bash
# Pull Prometheus, Loki and a page of Tempo traces into homelab/data/collected.
# Parsing is done by homelab.collect, which does not open a socket.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

if [[ "${1:-}" == "--print-queries" ]]; then
  py -m homelab.collect query --kind alerts
  while IFS=$'\t' read -r cid _app ns; do
    py -m homelab.collect query --kind memory --namespace "${ns}"
    printf 'company %s\n' "${cid}"
  done < <(py -m homelab.plan --companies "${HOMELAB_COMPANIES:-}" --tsv)
  exit 0
fi

once=0
if [[ "${1:-}" == "--once" ]]; then
  once=1
fi

ensure_dirs
state="${DATA}/collect-state.json"
companies_file="${HOMELAB}/companies.yaml"

while true; do
  if [[ ! -f "${DATA}/endpoints.env" ]]; then
    echo "collector: ${DATA}/endpoints.env is missing" >&2
    if [[ "${once}" == "1" ]]; then
      exit 1
    fi
    sleep 10
    continue
  fi
  # shellcheck disable=SC1091
  source "${DATA}/endpoints.env"
  now="$(date -u +%s)"
  if [[ -f "${state}" ]]; then
    start="$(py -c 'import json,sys; print(max(0, int(json.load(open(sys.argv[1]))["last_end"]) - 300))' "${state}")"
  else
    start="$((now - 7 * 24 * 3600))"
  fi
  # Prometheus refuses query_range above 11,000 points (HTTP 400); at step=60s
  # that is about 7.6 days. Clamp so a cold start or a long outage still loads.
  oldest="$((now - 7 * 24 * 3600))"
  if (( start < oldest )); then
    echo "collector: clamping start from ${start} to ${oldest} (Prometheus 11,000-point limit)" >&2
    start="${oldest}"
  fi

  alert_query="$(py -m homelab.collect query --kind alerts)"
  if curl -fsS --get "${PROM_URL}/api/v1/query_range" \
    --data-urlencode "query=${alert_query}" \
    --data-urlencode "start=${start}" \
    --data-urlencode "end=${now}" \
    --data-urlencode "step=60s" \
    -o "${DATA}/collected/alerts.json"; then
    py -m homelab.collect ingest --kind alerts \
      --input "${DATA}/collected/alerts.json" \
      --out "${DATA}/collected/alerts.csv" \
      --companies-file "${companies_file}"
  else
    echo "collector: prometheus alert query failed" >&2
  fi

  while IFS=$'\t' read -r cid _app ns; do
    mem_query="$(py -m homelab.collect query --kind memory --namespace "${ns}")"
    if curl -fsS --get "${PROM_URL}/api/v1/query_range" \
      --data-urlencode "query=${mem_query}" \
      --data-urlencode "start=${start}" \
      --data-urlencode "end=${now}" \
      --data-urlencode "step=60s" \
      -o "${DATA}/collected/memory-${cid}.json"; then
      py -m homelab.collect ingest --kind metrics \
        --input "${DATA}/collected/memory-${cid}.json" \
        --out "${DATA}/collected/metrics.csv" \
        --companies-file "${companies_file}"
    else
      echo "collector: memory query failed for ${cid}" >&2
    fi
  done < <(py -m homelab.plan --companies "${HOMELAB_COMPANIES:-}" --tsv)

  if [[ -n "${LOKI_URL:-}" ]]; then
    if curl -fsS --get "${LOKI_URL}/loki/api/v1/query_range" \
      --data-urlencode 'query={namespace=~"company-.+"}' \
      --data-urlencode "start=${start}000000000" \
      --data-urlencode "end=${now}000000000" \
      --data-urlencode "limit=2000" \
      -o "${DATA}/collected/logs.json"; then
      py -m homelab.collect ingest --kind logs \
        --input "${DATA}/collected/logs.json" \
        --out "${DATA}/collected/logs.csv" \
        --companies-file "${companies_file}" || true
    fi
  fi

  # Tempo keeps a 10% sample. Each pass stores one page of recent traces, not every span.
  if [[ -n "${TEMPO_URL:-}" ]]; then
    if curl -fsS --get "${TEMPO_URL}/api/search" \
      --data-urlencode "start=${start}" \
      --data-urlencode "end=${now}" \
      --data-urlencode "limit=20" \
      -o "${DATA}/collected/tempo-search.json"; then
      while read -r trace_id; do
        [[ -n "${trace_id}" ]] || continue
        if curl -fsS -H 'Accept: application/json' \
          "${TEMPO_URL}/api/traces/${trace_id}" \
          -o "${DATA}/collected/trace.json"; then
          py -m homelab.collect ingest --kind spans \
            --input "${DATA}/collected/trace.json" \
            --out "${DATA}/collected/spans.csv" || true
        fi
      done < <(py -m homelab.collect ids --input "${DATA}/collected/tempo-search.json")
    fi
  fi

  py -c 'import json,sys; json.dump({"last_end": int(sys.argv[1])}, open(sys.argv[2], "w"))' "${now}" "${state}"
  if [[ "${once}" == "1" ]]; then
    exit 0
  fi
  sleep 60
done
