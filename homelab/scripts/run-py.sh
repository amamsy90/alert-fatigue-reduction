#!/usr/bin/env bash
# Interpreter for a long-running lab process. with-inhibit.sh execs this file,
# so it cannot be the py() shell function.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=homelab/scripts/lib.sh
source "${HERE}/lib.sh"
export PYTHONPATH="${ROOT}:${ROOT}/app${PYTHONPATH:+:${PYTHONPATH}}"
if [[ -n "${HOMELAB_PYTHON:-}" ]]; then
  exec "${HOMELAB_PYTHON}" "$@"
fi
if command -v uv >/dev/null 2>&1 && [[ -f "${ROOT}/app/pyproject.toml" ]]; then
  exec uv run --directory "${ROOT}/app" --with pyyaml python "$@"
fi
exec python3 "$@"
