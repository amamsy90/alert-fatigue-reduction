# Architecture decision records

Each record states one decision, why it was made, and what it costs. Format: context, decision, consequences, status. Records are not edited after they are accepted; a later decision supersedes an earlier one by number.

| # | Decision | Status |
|---|---|---|
| [0001](0001-causal-first-come-queue.md) | A causal, first-come daily queue instead of ranking a day's items | Accepted |
| [0002](0002-shadow-by-default-live-behind-gates.md) | Shadow mode by default; Live paging behind four gates | Accepted |
| [0003](0003-single-node-sqlite-and-ops-file.md) | One process with SQLite and a locked JSON file for operator state | Accepted |
| [0004](0004-jump-over-recent-median.md) | Detect jumps against the recent median, not mean + k·σ | Accepted |
| [0005](0005-cost-sensitive-logistic-regression.md) | Cost-sensitive logistic regression for alerts, rules when there are no labels | Accepted |
| [0006](0006-report-numbers-checked-by-script.md) | The report's numbers are a table that a script checks | Accepted |
| [0007](0007-locked-dependencies-with-uv.md) | Lock dependencies with uv; hash-pinned install in Docker | Accepted |
