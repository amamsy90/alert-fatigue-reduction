# Retrospective

**Period:** 23–28 September 2026 · **Written:** after Phase 1 of the review plan

This is an honest account of how the project was built, what the reviews found, and how the work is run from now on. Facts come from the git history and the review documents; the *Author's notes* at the end are for the author to complete in their own words.

---

## 1. What the history shows

| Date | Commits | What happened |
|---|---:|---|
| 23 Sep | 1 | First version committed in one go: GAIA loader, classifier, jump detector, policy, web app |
| 24 Sep | 3 | Replaced a 95% target with walk-forward statistics; corrected the fold-7 story |
| 26 Sep | 8 | First code review (`CODE_REVIEW.md`); causal queue, fair baselines, gap items, *Evaluate my data* |
| 27 Sep | 11 | Connectors (PagerDuty, Slack, Opsgenie, Prometheus), shadow mode, hardening, SMD, Docker |
| 28 Sep | 5+ | Second review: security, reliability and look-ahead fixes; packaging and CI; this documentation |

About 30,000 lines arrived in five days, most of the design work before the first commit. There were no issues, branches or pull requests before the review, so the git history does not show the iterations that happened on the author's machine.

## 2. What went well

- **Reviews changed the claims, not just the code.** The first review showed the baseline was a straw man (log severity catches 0%) and that the queue ranked by hindsight. The report now compares against a Prometheus rate rule and uses a causal queue.
- **Negative results are reported.** The full queue now ties the rate rule week by week; the report says so and keeps the claim that holds (the detector wins 8/8 weeks).
- **Tests grew with the fixes:** 46 → 150 → 186, including tests that fail if a decision starts to use later data.
- **Reproducibility.** Every number comes from a script, and since Phase 0 a checker ties the report to the result files.

## 3. What went wrong

- **Look-ahead crept back in.** After the first review removed the main hindsight ranking, five smaller uses of later data remained in helper code while the text said "no look-ahead". A test that adds later data and checks earlier decisions did not change would have caught them; `test_causality.py` now does.
- **The checker checked itself.** `run_validate.py` compared results to numbers typed into its own source, so the report could drift unnoticed (ADR 0006).
- **Security followed features.** Live paging, keys and a Prometheus URL were added before a threat model existed. The second review found an SSRF bypass, open access behind a proxy, and a way to page without a password.
- **Scope grew late.** Slack, Opsgenie, SMD and a pilot offer were added on the last day, which left less time for tests of the core (`src/experiment.py` still has none).
- **Process was invisible.** Large commits, no issues and no CI make the work hard for anyone else to follow or review.

## 4. What changes from now on

1. **Every change goes through a pull request** with a short description and green CI (lint, types, tests on two Python versions, coverage, report check, Docker).
2. **Remaining work is tracked as GitHub issues**, one per item in Phases 2–3 and one per residual risk in `docs/threat_model.md`.
3. **A decision that shapes the design gets an ADR** before the code.
4. **A security-relevant feature gets a threat-model row and a test in the same change.**
5. **The mypy error budget only goes down** (39 at the start of CI).
6. **Releases are tagged** and summarised in `CHANGELOG.md`.

## 5. Author's notes

*To be completed by the author: what you would do differently at the start, what you learned about estimating, and which review finding surprised you most.*
