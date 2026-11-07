#!/usr/bin/env bash
# Deploy the on-call queue to a Kubernetes cluster with Helm, verify it, and roll back if it fails.
#
#   ONCALL_PASSWORD=... tools/deploy.sh <release> <namespace> [extra helm args]
#
# Steps, each a safeguard:
#   1. helm upgrade --install --atomic --wait: if the new pod never becomes ready, Helm rolls the
#      release back by itself.
#   2. Port-forward to the service and run tools/smoke_test.py: shadow mode on, password enforced,
#      monitoring, metrics, and (first install only) a sample job. An upgrade skips the sample job
#      so a live queue is not touched; set SMOKE_WRITE=1 to run it anyway.
#   3. If the smoke test fails, `helm rollback` to the previous revision (on a first install the
#      release is uninstalled instead). The data volume is kept either way.
#
# Environment:
#   ONCALL_PASSWORD   web app password (required unless EXISTING_SECRET is set)
#   EXISTING_SECRET   name of a Secret with APP_PASSWORD / INGEST_TOKEN; the password is read from it
#   IMAGE_TAG         image tag to deploy (default: the chart's v<appVersion>)
#   KUBE_CONTEXT      kubectl/helm context (default: the current one; always set it on a shared machine)
#   EXPECT_VERSION    version /health must report (default: the chart's appVersion)
#   LOCAL_PORT        port for the port-forward (default 18080)
#   PYTHON            interpreter for the smoke test (default: python)
set -euo pipefail

release="${1:?usage: tools/deploy.sh <release> <namespace> [helm args]}"
namespace="${2:?usage: tools/deploy.sh <release> <namespace> [helm args]}"
shift 2

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chart="$root/app/deploy/helm/oncall-queue"
port="${LOCAL_PORT:-18080}"
ctx=()
if [[ -n "${KUBE_CONTEXT:-}" ]]; then ctx=(--kube-context "$KUBE_CONTEXT"); fi
kctx=()
if [[ -n "${KUBE_CONTEXT:-}" ]]; then kctx=(--context "$KUBE_CONTEXT"); fi

app_version="$(grep -E '^appVersion:' "$chart/Chart.yaml" | sed -E 's/appVersion: *"?([^"]*)"?/\1/')"
expect="${EXPECT_VERSION:-$app_version}"

set_args=()
if [[ -n "${EXISTING_SECRET:-}" ]]; then
  set_args+=(--set-string "auth.existingSecret=$EXISTING_SECRET")
else
  : "${ONCALL_PASSWORD:?set ONCALL_PASSWORD (or EXISTING_SECRET)}"
  set_args+=(--set-string "auth.password=$ONCALL_PASSWORD")
fi
if [[ -n "${IMAGE_TAG:-}" ]]; then set_args+=(--set-string "image.tag=$IMAGE_TAG"); fi

first_install=1
if helm "${ctx[@]}" status "$release" --namespace "$namespace" >/dev/null 2>&1; then first_install=0; fi

echo "== helm upgrade --install $release (atomic)"
helm "${ctx[@]}" upgrade --install "$release" "$chart" --namespace "$namespace" --create-namespace \
  --atomic --wait --timeout 5m "${set_args[@]}" "$@"

service="$(kubectl "${kctx[@]}" -n "$namespace" get svc -l "app.kubernetes.io/instance=$release" -o name | head -n1)"
password="${ONCALL_PASSWORD:-}"
if [[ -n "${EXISTING_SECRET:-}" ]]; then
  password="$(kubectl "${kctx[@]}" -n "$namespace" get secret "$EXISTING_SECRET" -o jsonpath='{.data.APP_PASSWORD}' | base64 -d)"
fi

kubectl "${kctx[@]}" -n "$namespace" port-forward "$service" "$port:8080" >/dev/null 2>&1 &
forward=$!
trap 'kill $forward 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

smoke=("${PYTHON:-python}" "$root/tools/smoke_test.py" "http://127.0.0.1:$port" --user "${APP_USER:-oncall}" --password "$password" --expect-version "$expect")
if [[ "$first_install" -eq 0 && "${SMOKE_WRITE:-0}" != "1" ]]; then smoke+=(--no-write); fi

echo "== smoke test"
if "${smoke[@]}"; then
  echo "== deployed and verified: $release $expect"
  exit 0
fi

echo "== smoke test FAILED: rolling back" >&2
if [[ "$first_install" -eq 1 ]]; then
  helm "${ctx[@]}" uninstall "$release" --namespace "$namespace"
  echo "first install removed; the data volume is kept" >&2
else
  helm "${ctx[@]}" rollback "$release" --namespace "$namespace" --wait --timeout 5m
  echo "rolled back to the previous revision" >&2
fi
exit 1
