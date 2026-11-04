#!/usr/bin/env bash
# Delete the cluster and homelab/data. This removes collected telemetry and ground truth.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

"${HERE}/down.sh"
if command -v k3d >/dev/null 2>&1; then
  "${HERE}/cluster-down.sh"
else
  log "k3d is not installed; skipped cluster delete"
fi
if [[ -d "${DATA}" ]]; then
  rm -rf "${DATA}"
  log "removed ${DATA}"
fi
log "teardown finished"
