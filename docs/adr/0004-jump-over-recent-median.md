# 0004 — Jumps against the recent median, not mean + k·σ

**Status:** Accepted

## Context
The first detector flagged memory above the training week's mean plus three standard deviations. When a service's normal level drifted (a restart, a slow leak), it flagged that service for days. On GAIA it overlapped 54.2% of test faults.

## Decision
A point is hot when it rises more than a set amount above the median of the previous 15 minutes on the same series (absolute mode, 0.5 GB chosen on week 1 only). Relative mode uses a share of the median; robust mode uses a rolling interquartile range, so any unit works. A jump is at least two hot points no more than a gap apart and is confirmed at its second point. A separate slow-leak rule uses a 3-hour median.

## Consequences
- 86.1% overlap with test faults, 99.6% where metric data exists; median 0.72 minutes from fault start to confirmation.
- Faults smaller than the jump size are invisible (a 0.25 GB step: 1%). A team must set the size for its own services, or let Evaluate learn one per series.
- Ramps and leaks defeat the 15-minute rule and a Prometheus rate rule; the leak rule catches them (99% of 30-minute ramps).
