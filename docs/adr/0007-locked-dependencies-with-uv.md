# 0007 — Locked dependencies with uv

**Status:** Accepted

## Context
`requirements.txt` listed version ranges, with the tested versions only in comments. Two builds a month apart could install different pandas or scikit-learn versions and give different numbers. The Docker image installed the same ranges.

## Decision
- `app/pyproject.toml` declares the ranges; `app/uv.lock` pins the exact versions for every supported Python (3.10–3.12).
- The Docker image installs `app/requirements.lock`, exported from `uv.lock` with hashes (`pip install --require-hashes`).
- CI fails if `requirements.lock` is out of date with `uv.lock`, and installs with `uv sync --locked`.
- `requirements.txt` stays for readers who use plain pip.

## Consequences
- Builds and experiment reruns are repeatable; a dependency upgrade is an explicit `uv lock --upgrade` change that CI tests.
- A developer needs `uv` for the dev tools (`uv sync --group dev`).
- Dependabot (`uv` updater) rewrites `uv.lock` but not `requirements.lock`. `.github/workflows/dependabot-lock.yml` regenerates `requirements.lock` on Dependabot's branches and reruns CI on the new commit. Once it has pushed, Dependabot no longer rebases that PR by itself; comment `@dependabot recreate` if it falls behind `master`.
