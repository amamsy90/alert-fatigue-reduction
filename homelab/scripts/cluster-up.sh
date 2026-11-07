#!/usr/bin/env bash
# Create the single-node k3d cluster if it does not already exist.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

require_cmd k3d
require_cmd kubectl

if k3d cluster get "${CLUSTER_NAME}" >/dev/null 2>&1; then
  log "cluster ${CLUSTER_NAME} already exists"
else
  k3d cluster create "${CLUSTER_NAME}" \
    --servers 1 \
    --agents 0 \
    --k3s-arg "--disable=traefik@server:0" \
    --wait
  log "created cluster ${CLUSTER_NAME}"
fi
kubectl config use-context "k3d-${CLUSTER_NAME}" >/dev/null
kubectl cluster-info >/dev/null
