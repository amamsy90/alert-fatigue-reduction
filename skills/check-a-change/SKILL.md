---
name: check-a-change
description: Run the same checks CI runs, in the same order, before committing a change to this repository.
---

# Check a change

Run from `app/`. On OneDrive or another synced folder, `uv` may fail to hard-link; set `UV_LINK_MODE=copy`.

1. `uv sync --group dev`
2. `uv run ruff check .` — lint, import order, bugbear, and McCabe complexity (limit 15). Fix findings; do not add `noqa` to pass.
3. `uv run mypy` — must report no issues.
4. `uv run pytest -q` — all tests that can run must pass. The Docker integration test skips unless `INTEGRATION_BASE_URL` is set.
5. If you touched a result file or the long report: `uv run python run_validate.py` — every checked number must match.

Before `git add`:

- Stage files by name. Never `git add -A` at a folder that can contain a clone or a backup.
- Keep out `.env`, `ops.json`, `app/product/data/`, `*.pkl`, and `*.db`.
