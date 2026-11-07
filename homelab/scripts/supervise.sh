#!/usr/bin/env bash
# Run a command and restart it whenever it exits. Used for port-forwards and the
# collect, score and chaos loops so one crash does not stop collection.
# usage: supervise.sh NAME command...
# HOMELAB_SUPERVISE_MAX_RESTARTS (default unlimited), HOMELAB_SUPERVISE_BACKOFF_START
# (default 2 s) and HOMELAB_SUPERVISE_BACKOFF_MAX (default 60 s) exist for tests.
set -uo pipefail
if [[ $# -lt 2 ]]; then
  echo "usage: supervise.sh NAME command..." >&2
  exit 2
fi
name="$1"
shift
max_restarts="${HOMELAB_SUPERVISE_MAX_RESTARTS:-0}"
backoff_max="${HOMELAB_SUPERVISE_BACKOFF_MAX:-60}"
backoff_start="${HOMELAB_SUPERVISE_BACKOFF_START:-2}"
backoff="${backoff_start}"
restarts=0
child=""
stop() {
  if [[ -n "${child}" ]]; then
    kill "${child}" 2>/dev/null || true
  fi
  exit 0
}
trap stop TERM INT HUP
while true; do
  started="$(date +%s)"
  "$@" &
  child=$!
  wait "${child}"
  code=$?
  child=""
  restarts=$((restarts + 1))
  if [[ "${max_restarts}" != "0" && "${restarts}" -gt "${max_restarts}" ]]; then
    echo "$(date -u +%FT%TZ) supervise: ${name} exited ${code}; restart limit reached" >&2
    exit "${code}"
  fi
  # A run that lasted over a minute was healthy; start the backoff again.
  if (( $(date +%s) - started > 60 )); then
    backoff="${backoff_start}"
  fi
  echo "$(date -u +%FT%TZ) supervise: ${name} exited ${code}; restart ${restarts} in ${backoff}s" >&2
  sleep "${backoff}" &
  wait $!
  backoff=$((backoff * 2))
  if (( backoff > backoff_max )); then
    backoff="${backoff_max}"
  fi
done
