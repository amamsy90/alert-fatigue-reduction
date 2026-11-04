#!/usr/bin/env bash
# Bring the lab up, or render only with --dry-run.
# A second run is safe: the cluster, charts and deployments are upgraded in place,
# and the chaos scheduler resumes from homelab/data/chaos-state.json.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

DRY_RUN=0
HOMELAB_COMPANIES=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --companies)
      HOMELAB_COMPANIES="${2:-}"
      shift 2
      ;;
    --companies=*)
      HOMELAB_COMPANIES="${1#*=}"
      shift
      ;;
    -h|--help)
      echo "usage: up.sh [--dry-run] [--companies acme,borealis]"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
export HOMELAB_COMPANIES

render_all
if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run: wrote ${RENDER} and did not touch a cluster"
  exit 0
fi

require_cmd docker
require_cmd k3d
require_cmd kubectl
require_cmd helm
require_cmd curl

"${HERE}/cluster-up.sh"
kubectl apply -f "${RENDER}/namespaces.yaml"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
helm repo update

helm_release kps prometheus-community/kube-prometheus-stack "${KPS_CHART_VERSION}" \
  observability "${HOMELAB}/config/kps-values.yaml" 15m

kubectl apply -f "${RENDER}/loki.yaml"
kubectl apply -f "${RENDER}/tempo.yaml"
kubectl apply -f "${RENDER}/otel-collector.yaml"
kubectl apply -f "${RENDER}/alloy.yaml"
kubectl -n observability rollout status deploy/loki --timeout=180s
kubectl -n observability rollout status deploy/tempo --timeout=180s
kubectl -n observability rollout status deploy/otel-collector --timeout=180s
kubectl -n observability rollout status deploy/alloy --timeout=180s

socket_args=()
if [[ -n "${HOMELAB_CONTAINERD_SOCKET:-}" ]]; then
  socket_args+=(--set "chaosDaemon.socketPath=${HOMELAB_CONTAINERD_SOCKET}")
fi
helm_release chaos-mesh chaos-mesh/chaos-mesh "${CHAOS_CHART_VERSION}" \
  chaos-mesh "${HOMELAB}/config/chaos-values.yaml" 10m "${socket_args[@]}"

while IFS=$'\x1f' read -r action cid ns url rewrite release chart version; do
  [[ -n "${action}" ]] || continue
  log "deploy ${cid} (${action})"
  kubectl create namespace "${ns}" --dry-run=client -o yaml | kubectl apply -f -
  case "${action}" in
    kubectl-apply-url)
      if [[ -n "${rewrite}" ]]; then
        curl -fsSL "${url}" | py -m homelab.plan --rewrite-from "${rewrite}" --rewrite-to "${ns}" | kubectl apply -f -
      else
        kubectl apply -n "${ns}" -f "${url}"
      fi
      if [[ -f "${RENDER}/loadgenerator-${cid}.yaml" ]]; then
        kubectl apply -f "${RENDER}/loadgenerator-${cid}.yaml"
      fi
      ;;
    helm)
      helm_release "${release}" "${chart}" "${version}" "${ns}" \
        "${RENDER}/otel-demo-${cid}.yaml" 10m
      ;;
    *)
      echo "unknown deploy action: ${action}" >&2
      exit 1
      ;;
  esac
  names="$(kubectl -n "${ns}" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{","}{end}')"
  py -m homelab.workloads --company "${cid}" --names "${names}"
done < <(py -c 'import json,sys; plan=json.load(open(sys.argv[1]));
[print("\x1f".join([s.get("action",""), s.get("company",""), s.get("namespace",""), s.get("url",""), s.get("rewrite_from",""), s.get("release",""), s.get("chart",""), s.get("version","")])) for s in plan["steps"]]' "${RENDER}/plan.json")

kubectl apply -f "${RENDER}/rules.yaml"

log "building oncall-queue:homelab"
docker build -t oncall-queue:homelab "${ROOT}/app"
k3d image import oncall-queue:homelab -c "${CLUSTER_NAME}"
kubectl apply -f "${RENDER}/oncall-queue.yaml"
kubectl -n observability rollout status deploy/oncall-queue --timeout=180s

start_forward prom observability kps-prometheus 9090 9090
start_forward am observability kps-alertmanager 9093 9093
start_forward loki observability loki 3100 3100
start_forward tempo observability tempo 3200 3200
start_forward grafana observability kps-grafana 3000 80
start_forward app observability oncall-queue 8080 8080

cat > "${DATA}/endpoints.env" <<'EOF'
PROM_URL=http://127.0.0.1:9090
LOKI_URL=http://127.0.0.1:3100
TEMPO_URL=http://127.0.0.1:3200
APP_URL=http://127.0.0.1:8080
EOF

ready=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8080/health" >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "${ready}" != "1" ]]; then
  echo "the app did not answer /health on 127.0.0.1:8080" >&2
  exit 1
fi
curl -fsS "http://127.0.0.1:8080/connectors" -o "${DATA}/connectors.json"
py -m homelab.safety --connectors "${DATA}/connectors.json"

start_bg chaos "${HERE}/supervise.sh" chaos "${HERE}/with-inhibit.sh" "${HERE}/run-py.sh" -m homelab.scheduler \
  --state "${DATA}/chaos-state.json" \
  --faults "${DATA}/faults.csv" \
  --companies "${HOMELAB_COMPANIES}"
start_bg collect "${HERE}/supervise.sh" collect "${HERE}/with-inhibit.sh" "${HERE}/collect-loop.sh"
start_bg score "${HERE}/supervise.sh" score "${HERE}/with-inhibit.sh" "${HERE}/score-loop.sh"

log "lab is up in shadow mode"
log "UI: http://127.0.0.1:8080   Grafana: http://127.0.0.1:3000 (admin / homelab)"
log "check: ${HERE}/status.sh    stop: ${HERE}/down.sh    resume: ${HERE}/up.sh"
