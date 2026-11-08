# AGENTS.md — AI-Assisted On-Call Queue

Stable rules for any coding agent working in this repository. Put the current task in the prompt, not in this file.

## What this project is

A solo software-engineering course project. It builds one daily on-call queue of at most K items for a cloud platform, and checks that queue against injected faults in the GAIA dataset. The grade is on the software around the model: requirements, tests, deployment, model versions, and monitoring.

## Rules that do not change

1. **Time split only.** Train on the first week. Test on the later days. Never shuffle rows into a random train and test.
2. **No future features.** A feature must be knowable at the moment the queue decides. Realised fault labels are for scoring, not for the decision.
3. **Models are a skops archive.** `load_model` refuses pickle, even when the file is still named `model.pkl` and the hash matches. Do not call `pickle` or `joblib` on a model file.
4. **Shadow is the default.** A job records what it would have paged. Live paging needs `APP_PASSWORD`, a channel, and the typed word `LIVE`, and only ingest jobs page.
5. **Do not commit secrets or runtime state.** Leave out `.env`, `ops.json`, `app/product/data/`, `*.pkl`, and `*.db`.
6. **Do not commit the other course.** Leave `course_project_proposal.md`, `Project_Proposal_Instructions.pdf`, and `lectures/` untracked.

## How to check a change

From `app/`:

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check .          # also fails any function with more than 15 independent paths (McCabe)
uv run mypy
```

Step-by-step instructions for the recurring jobs (checking a change, adding a regression test, review and simplification, reproducing results, the live soak, cutting a release) are in [`skills/`](skills/README.md).

A behaviour change needs a regression test whose expected result comes from the requirement, not from the function under test.
