# 0002 — Shadow mode by default; Live paging behind four gates

**Status:** Accepted

## Context
The tool sits next to a real pager. A wrong page at 3 a.m. costs trust, and a demo that pages real people is worse. Teams also want to judge the tool before it can page anyone.

## Decision
Shadow mode is the default: every job is scored and stored, and records what Live would have paged, but nothing is sent. A page is sent only when all four hold:
1. Live mode is on (typed `LIVE` on the Connectors page, or `SHADOW_MODE=0`);
2. at least one channel is configured (PagerDuty, Slack or Opsgenie);
3. a login password (`APP_PASSWORD`) is set — checked on the switch and again in `ops.can_page()`, so the environment route cannot skip it;
4. the job is a webhook-ingest job — sample, GAIA, upload and Evaluate runs never page.

Only items shown since the last job are paged, per channel, so switching to Live never pages the shadow period and a retry re-sends only to a channel that failed.

## Consequences
- A team can run the tool for weeks and compare *would have paged* with what happened.
- Live needs an operator decision and a password; the audit log records who switched it.
- A jump whose points reach Prometheus after the next job's mark can be skipped (known limitation, see the threat model).
