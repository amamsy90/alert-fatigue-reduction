#!/usr/bin/env bash
# Delete the k3d cluster. Does not delete homelab/data; teardown.sh does both.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

require_cmd k3d
if k3d cluster get "${CLUSTER_NAME}" >/dev/null 2>&1; then
  k3d cluster delete "${CLUSTER_NAME}"
  log "deleted cluster ${CLUSTER_NAME}"
else
  log "cluster ${CLUSTER_NAME} is already gone"
fi
