# 0001 — A causal, first-come daily queue

**Status:** Accepted (revised after the Phase 0 review)

## Context
The first version ranked each day's jumps by their final peak and kept the top K. That needs the whole day in advance, so an on-call engineer could never get that list live. A later review found five smaller places that still used later data (per-day alert top-K, leak removal by a later jump, a whole-series spread in robust mode, an alert attached using a jump's final end, gap merging).

## Decision
Every item is shown at the moment a live system could know about it: a jump at its confirmation point, a gap `gap_minutes` after the last point, an alert at its timestamp. Each day holds at most K items, first come, first served. Log alerts may use at most `alert_share` of K. An alert rides on a jump only while `Spike.open_at` says the jump is still open, which needs points up to the alert only. Nothing decides using a later point, a jump's end or its peak.

## Consequences
- The queue is usable live, and the evaluation measures what a person would actually have seen.
- A late, bigger item can lose its slot to an earlier, smaller one. Replacing the peak ranking with a threshold learned on week 1 kept the detector's score (72.6% vs 72.9% with hindsight).
- Removing the last look-ahead cost the full queue 0.6 points (70.2% → 69.6%) and its weekly win over the rate rule (now 4–3–1).
- `tests/test_causality.py` fails if a decision starts to depend on later data.
