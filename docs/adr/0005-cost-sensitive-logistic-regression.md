# 0005 — Cost-sensitive logistic regression for alerts

**Status:** Accepted

## Context
Log alerts need a score, but a new team has no labels, and the GAIA weights do not transfer to another company. A missed fault costs far more than a false alarm.

## Decision
- Alerts are scored with logistic regression on TF-IDF text, source, severity and recent counts, with a miss weighted 10× a false alarm. The escalation threshold is the training-window score with the lowest cost.
- With no labels the queue runs on rules only (jumps, gaps, severity with duplicate suppression) and says so on the page.
- Real / Noise marks on shown items become labels for later runs; a model is saved (with a SHA-256 file that refuses a changed model) and reused for small webhook batches, with a retrain reminder after 7 days or new Real marks.

## Consequences
- Fast, explainable and trainable on a laptop; one model per team.
- The model helps little on GAIA, because 82% of faults never write a log; the metric path does most of the work there.
- Pickle files are only as safe as the directory they live in; the hash check guards against a changed file, not a malicious first file.
