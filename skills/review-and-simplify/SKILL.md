---
name: review-and-simplify
description: Review a diff for correctness and keep functions small; split anything ruff reports as too complex.
---

# Review and simplify

Review order: correctness first, then clarity, then cleanup.

1. Read every changed line and the whole function around it. Ask what input, state or timing makes the line wrong. Check callers of any changed function.
2. For deleted code, name the guard it provided and find where the new code keeps it.
3. Look for look-ahead: a feature or threshold that uses information from after the moment the queue decides breaks `AGENTS.md` rule 2.
4. Complexity: `uv run ruff check .` fails any function over 15 independent paths. To fix one:
   - Cut it at its natural sections (setup, one decision, one output) and move each to a named helper with a one-line docstring.
   - Pass data in and return it out; do not share state through closures.
   - Prove nothing changed: run the tests around it, and for `run_validate.py` compare `computed_numbers()` before and after (it must be identical).
   - Do not silence the rule with `noqa` or a higher limit.
5. Claims in docs and the report must match results. A sentence that depends on a number goes through `run_validate.py`; a sentence that can stop being true gets a guard in `claims_still_hold`.
6. Report findings with a file, a line and a concrete failing scenario. Say what was not checked.
