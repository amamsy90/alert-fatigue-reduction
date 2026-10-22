#!/usr/bin/env bash
# Shared helpers for the homelab scripts. Source this file; do not execute it.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "source homelab/scripts/lib.sh; do not execute it" >&2
  exit 1
fi

HOMELAB_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOMELAB="$(cd "${HOMELAB_SCRIPTS}/.." && pwd)"
ROOT="$(cd "${HOMELAB}/.." && pwd)"
# HOMELAB_DATA lets tests use a scratch directory instead of the live data.
DATA="${HOMELAB_DATA:-${HOMELAB}/data}"
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

# Put ROOT and ROOT/app on PYTHONPATH once. py() runs thousands of times in the
# long-lived loops; prepending on every call grew the variable past the kernel's
# 128 KiB per-string limit and exec failed with "Argument list too long".
homelab_pythonpath() {
  local entry
  for entry in "${ROOT}/app" "${ROOT}"; do
    case ":${PYTHONPATH:-}:" in
      *":${entry}:"*) ;;
      *) PYTHONPATH="${entry}${PYTHONPATH:+:${PYTHONPATH}}" ;;
    esac
  done
  export PYTHONPATH
}

py() {
  homelab_pythonpath
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
  # Stop the parent first so a supervisor cannot restart a child we just killed.
  local pid="$1"
  local child
  local children
  children="$(ps -o pid= --ppid "${pid}" 2>/dev/null || true)"
  kill "${pid}" 2>/dev/null || true
  for child in ${children}; do
    kill_tree "${child}"
  done
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
  # kubectl port-forward exits when the pod behind the service restarts, so it
  # runs under supervise.sh and comes back on its own.
  start_bg "${name}" "${HOMELAB_SCRIPTS}/supervise.sh" "${name}" \
    kubectl -n "${namespace}" port-forward --address 127.0.0.1 \
    "svc/${service}" "${local_port}:${remote_port}"
}

# helm_release RELEASE CHART VERSION NAMESPACE VALUES_FILE TIMEOUT [--set k=v ...]
# Skips the upgrade when the release is deployed with the same chart version and
# values (see homelab/helmstate.py). Otherwise upgrades with a bounded timeout and
# rolls back on failure, so an interrupted upgrade does not leave a half-applied
# revision behind.
helm_release() {
  local release="$1" chart="$2" version="$3" namespace="$4" values="$5" timeout="$6"
  shift 6
  local extra=("$@")
  local set_args=()
  local i
  for ((i = 0; i < ${#extra[@]}; i++)); do
    if [[ "${extra[i]}" == "--set" ]]; then
      set_args+=(--set "${extra[i + 1]}")
      i=$((i + 1))
    fi
  done
  local verdict
  verdict="$(py -m homelab.helmstate --release "${release}" --namespace "${namespace}" \
    --chart-version "${version}" --values "${values}" "${set_args[@]}" || echo upgrade)"
  if [[ "${verdict}" == "skip" && "${HOMELAB_FORCE_HELM:-0}" != "1" ]]; then
    log "helm ${release}: unchanged, skipping upgrade (HOMELAB_FORCE_HELM=1 forces it)"
    return 0
  fi
  local flags=()
  if helm version --short 2>/dev/null | grep -q '^v4\.'; then
    # Helm 4 applies server-side; homelab.workloads scales deployments after each
    # deploy, so an upgrade must take ownership of .spec.replicas back.
    flags+=(--force-conflicts --rollback-on-failure)
  else
    flags+=(--atomic)
  fi
  helm upgrade --install "${release}" "${chart}" \
    --version "${version}" \
    --namespace "${namespace}" \
    --create-namespace \
    --values "${values}" \
    "${extra[@]}" \
    "${flags[@]}" \
    --wait --timeout "${timeout}"
}
