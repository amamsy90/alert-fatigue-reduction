---
name: add-a-regression-test
description: Write a test for a behaviour change or bug fix whose expected value comes from the requirement, not from the code under test.
---

# Add a regression test

1. State the requirement in one sentence, in words a grader can read ("a second fault on a service must not start inside the 15-minute window").
2. Work out the expected result by hand or from the requirement, before looking at what the function returns. Example: a hand-counted delay of 90 s, not `assert f(x) == f(x)` and not a value copied from today's output.
3. Put the test beside its neighbours in `app/tests/`, named for the behaviour (`test_a_second_fault_...`), with a docstring that gives the incident or requirement.
4. Prove it can fail: run it against the old code, or change the code on purpose, and see it go red. Then restore.
5. Keep the rules from `AGENTS.md`: time split only (train on the first week, test later), no future features at decision time, models are skops archives (no `pickle`).
6. Use temporary paths and patch with `monkeypatch`, so the test leaves no global state behind (a patched `time.time` must be restored).
7. Run `check-a-change`.

A refactor with no change in behaviour needs no new test, but the existing tests and, for report numbers, `run_validate.py` must pass unchanged.
