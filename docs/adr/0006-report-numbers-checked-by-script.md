# 0006 — The report's numbers are a table that a script checks

**Status:** Accepted (replaces numbers hard-coded in `run_validate.py`)

## Context
`run_validate.py` used to compare the result files against numbers typed into its own source. It never read the report, so the report could drift from the results without any check failing. That happened: after the look-ahead fixes the checker failed while the report still showed the old numbers.

## Decision
`FINAL_REPORT.md` ends with a *Checked numbers* table (key, printed value, section). `run_validate.py` recomputes every key from `app/results/` and fails when a value no longer rounds to what is printed, when the report checks a key it cannot compute, or when it can compute a key the report leaves out. Claims that are not one number (for example "the detector wins 8/8 weeks") are checked separately. CI runs it on every push.

## Consequences
- A change that moves a result forces the report to be updated in the same change; the SMD refresh after Phase 0 was caught this way.
- Prose that repeats a number outside the table is still checked by a person; the table's *Where* column says where to look.
