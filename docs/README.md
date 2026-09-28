# Engineering documents

| Document | What it answers |
|---|---|
| [requirements.md](requirements.md) | What the system must do and how well (FR / NFR), user stories with acceptance criteria, and which test verifies each requirement |
| [architecture.md](architecture.md) | Context, components, the ingest-to-page sequence, deployment and the data model (Mermaid diagrams) |
| [adr/](adr/README.md) | Why the main design decisions were made, and what they cost |
| [threat_model.md](threat_model.md) | Assets, trust boundaries, STRIDE threats with mitigations and tests, residual risks |
| [test_plan.md](test_plan.md) | Test levels, how to run them, CI gates, measurable criteria, performance results, known gaps |
| [deployment.md](deployment.md) | Where it runs, development / test / production environments, scalability, AI governance |
| [mlops.md](mlops.md) | Versioning (code, config, data, models), reproducibility, model updates and the promotion gate, experiment tracking |
| [monitoring.md](monitoring.md) | `/metrics` and `/monitoring`: AI, system and data signals, targets, alert rules, evaluation criteria |
| [privacy_responsible_ai.md](privacy_responsible_ai.md) | What data is kept, where, for how long, who sees it; bias, oversight, misuse, safety |
| [retrospective.md](retrospective.md) | How the project was built, what the reviews found, and how the work is run from now on |

Related, outside this folder: `../design_specification.md` (component detail and configuration), `../FINAL_REPORT.md` (results), `../VALIDATION.md` (result files), `../CHANGELOG.md` (versions), `../.github/workflows/ci.yml` (CI).
