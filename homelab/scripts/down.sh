#!/usr/bin/env bash
# Stop collection, chaos, scoring and port-forwards. The cluster and homelab/data stay.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

for name in collect chaos score prom am loki tempo grafana app; do
  stop_pid "${name}"
done
log "stopped lab processes; cluster ${CLUSTER_NAME} and ${DATA} were kept"
log "resume with ${HOMELAB_SCRIPTS}/up.sh"
