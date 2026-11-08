---
name: reproduce-and-validate
description: Reproduce the reported results from a clean clone and check that every number in the long report still matches the committed result files.
---

# Reproduce and validate

1. Data: `python tools/fetch_gaia.py` downloads GAIA MicroSS (public). The real 10-day slice committed in `app/examples/gaia_sample/` is enough for `run_reproduce.py` with no download (`--full` needs the download). RCAEval, NAB and SMD have their own `tools/fetch_*.py`.
2. From `app/`: `uv sync --locked --group dev`, then `uv run python run_reproduce.py`.
3. `uv run python run_validate.py` reads the "Checked numbers" table in `FINAL_REPORT.md`, recomputes each key from `app/results/`, and fails on any mismatch or on a broken guarded claim.
4. When a result changes: rerun the script that wrote it, then edit the printed number in `FINAL_REPORT.md` and the key in the table, then rerun `run_validate.py`. Never edit a result file by hand to make a number match.
5. When you add a new kind of result: add its keys in `run_validate.computed_numbers` (one small helper per source file), its rows in the table, and a guard in `claims_still_hold` for any sentence that would become false.
6. Clean-clone proof: `python tools/check_fresh_clone.py --tests` clones into an empty folder, installs the locked dependencies, reproduces, and runs the suite. It ends with `PASS` or `FAIL`. It only works for files that are committed.

State honestly what the results do not show: GAIA has one injected fault shape; the log-alert classifier is refused by the gate on every RCAEval application; no real team has run the system.
