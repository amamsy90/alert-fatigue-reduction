#!/usr/bin/env bash
# Render manifests into homelab/data/render. Does not need a cluster.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"

HOMELAB_COMPANIES=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --companies)
      HOMELAB_COMPANIES="${2:-}"
      shift 2
      ;;
    --companies=*)
      HOMELAB_COMPANIES="${1#*=}"
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
export HOMELAB_COMPANIES
render_all
log "rendered into ${RENDER}"
