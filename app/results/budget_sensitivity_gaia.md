# Budget sensitivity

Dataset: gaia. Escalate when P(real) >= 0.560. Ask a person when 0.091 <= P(real) < 0.560. Scores below that floor are suppressed.

Confident faults take the daily slots first. Uncertain alerts use whatever slots remain. Recall held means the queue still catches every fault the confident-only policy caught.

| Budget K | Escalate | Ask a person | Suppress | Faults detected | Recall | False alerts/day | MTTA (min) | Recall held |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 5 | 11 | 264 | 30958 | 0.006 | 0.022 | 4.89 | 3.5 | yes |
| 10 | 11 | 536 | 30686 | 0.007 | 0.032 | 9.83 | 4.2 | yes |
| 20 | 11 | 1076 | 30146 | 0.017 | 0.063 | 19.18 | 4.5 | yes |
| 50 | 11 | 2696 | 28526 | 0.027 | 0.120 | 48.11 | 3.8 | yes |

Reading the table: a larger budget does not create more confident escalations. Those are fixed by the week-1 cutoff. Extra slots become questions for a person, which raises fault detection and also raises the number of false alerts they see.
