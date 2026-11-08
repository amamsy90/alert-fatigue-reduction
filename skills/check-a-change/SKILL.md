---
name: check-a-change
description: Run the same checks CI runs, in the same order, before committing a change to this repository.
---

# Check a change

Run from `app/`. On OneDrive or another synced folder, `uv` may fail to hard-link; set `UV_LINK_MODE=copy`.

1. `uv sync --group dev`
2. `uv run ruff check .` — lint, import order, bugbear, and McCabe complexity (limit 15). Fix findings; do not add `noqa` to pass.
3. `uv run mypy` — must report no issues.
4. `uv run pytest -q` — all tests that can run must pass. A few skip without torch, xgboost, CUDA or Docker; that is expected.
5. If you touched a result file or the long report: `uv run python run_validate.py` — every checked number must match.
6. If you touched the Helm chart, `Dockerfile` or `tools/deploy.sh`: `uv run pytest -q tests/test_helm_chart.py tests/test_smoke_script.py`.

Before `git add`:

- Stage files by name. Never `git add -A` at a folder that can contain a clone, a backup or `.claude/`.
- Keep out `.env`, `ops.json`, `app/product/data/`, `*.pkl`, `*.db`, `.claude/`, `.cursor/` (AGENTS.md rules 5 and 6).
- A file that must run on Linux (`tools/deploy.sh`) needs the executable bit in git: `git update-index --chmod=+x`.
