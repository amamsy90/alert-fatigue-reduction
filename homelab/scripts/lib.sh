#!/usr/bin/env bash
# Shared helpers for the homelab scripts. Source this file; do not execute it.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "source homelab/scripts/lib.sh; do not execute it" >&2
  exit 1
fi

HOMELAB_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOMELAB="$(cd "${HOMELAB_SCRIPTS}/.." && pwd)"
ROOT="$(cd "${HOMELAB}/.." && pwd)"
DATA="${HOMELAB}/data"
RENDER="${DATA}/render"
# shellcheck source=homelab/config/versions.env
source "${HOMELAB}/config/versions.env"

log() {
  printf '%s\n' "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing command: $1" >&2
    exit 1
  fi
}

py() {
  export PYTHONPATH="${ROOT}:${ROOT}/app${PYTHONPATH:+:${PYTHONPATH}}"
  if [[ -n "${HOMELAB_PYTHON:-}" ]]; then
    # The caller sets HOMELAB_PYTHON to one interpreter, not a shell snippet.
    "${HOMELAB_PYTHON}" "$@"
    return
  fi
  if command -v uv >/dev/null 2>&1 && [[ -f "${ROOT}/app/pyproject.toml" ]]; then
    uv run --directory "${ROOT}/app" --with pyyaml python "$@"
    return
  fi
  python3 "$@"
}

ensure_dirs() {
  mkdir -p "${DATA}/collected" "${DATA}/logs" "${DATA}/pids" "${RENDER}"
}

render_all() {
  ensure_dirs
  py -m homelab.plan --companies "${HOMELAB_COMPANIES:-}" --out "${RENDER}"
  py -m homelab.obs --companies "${HOMELAB_COMPANIES:-}" --out "${RENDER}"
}

kill_tree() {
  local pid="$1"
  local child
  while read -r child; do
    if [[ -n "${child}" ]]; then
      kill_tree "${child}"
    fi
  done < <(ps -o pid= --ppid "${pid}" 2>/dev/null || true)
  kill "${pid}" 2>/dev/null || true
}

stop_pid() {
  local name="$1"
  local pid_file="${DATA}/pids/${name}.pid"
  if [[ ! -f "${pid_file}" ]]; then
    return
  fi
  local pid
  pid="$(cat "${pid_file}")"
  if [[ -n "${pid}" ]]; then
    kill_tree "${pid}"
  fi
  rm -f "${pid_file}"
}

start_bg() {
  local name="$1"
  shift
  ensure_dirs
  local pid_file="${DATA}/pids/${name}.pid"
  if [[ -f "${pid_file}" ]]; then
    local old
    old="$(cat "${pid_file}")"
    if [[ -n "${old}" ]] && kill -0 "${old}" 2>/dev/null; then
      log "${name} already running (${old})"
      return
    fi
  fi
  "$@" >> "${DATA}/logs/${name}.log" 2>&1 &
  echo $! > "${pid_file}"
  log "started ${name} ($(cat "${pid_file}"))"
}

start_forward() {
  local name="$1"
  local namespace="$2"
  local service="$3"
  local local_port="$4"
  local remote_port="$5"
  start_bg "${name}" kubectl -n "${namespace}" port-forward --address 127.0.0.1 \
    "svc/${service}" "${local_port}:${remote_port}"
}
