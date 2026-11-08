# Project skills

Reusable, step-by-step instructions for a coding agent (or a person) working in this repository. Each folder holds one `SKILL.md` in the common Agent Skills layout: a short header with `name` and `description`, then the steps. They are kept here, not in a tool-specific folder, so any agent can read them. `AGENTS.md` at the repository root holds the rules that never change; these skills say how to do the recurring jobs without breaking them.

| Skill | Use it when |
|---|---|
| [check-a-change](check-a-change/SKILL.md) | You changed code and need to know it is safe to commit |
| [add-a-regression-test](add-a-regression-test/SKILL.md) | You changed behaviour or fixed a bug |
| [review-and-simplify](review-and-simplify/SKILL.md) | You review code, or ruff reports a function that is too complex |
| [reproduce-and-validate](reproduce-and-validate/SKILL.md) | You change a result, a number in the report, or the data setup |
| [run-the-live-soak](run-the-live-soak/SKILL.md) | You need real-time evidence from the Prometheus, Alertmanager and app stack |
| [cut-a-release](cut-a-release/SKILL.md) | You publish a version |

How they were used: the checks, the regression-test rule, the complexity limit, the soak and the release steps below are the ones used to build the 1.2.x releases. Failures met on the way (a release workflow that stopped three times, a non-executable script, a force-push that reverted `master`) are written into the steps they would have prevented.
