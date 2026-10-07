# Privacy and responsible AI

**System:** Budgeted Incident Detection · **Version:** 1.0.0

## 1. Privacy

| Question | Answer |
|---|---|
| **What information is collected?** | Alert records (time, service name, severity, alert type, message text), memory metrics (time, series, value), Real / Noise marks, and for the audit log the login user name and client IP of each change. No customer data is needed. |
| **Is sensitive information involved?** | Possibly, inside alert **message text**: log lines can contain host names, user IDs, e-mail addresses or request paths. Metrics and service names are operational, not personal. Keys for PagerDuty, Slack and Opsgenie are secrets. |
| **Where is it stored?** | On the team's own server: SQLite (`queue.db`), operator settings (`ops.json`), paging secrets (`secrets.json` or a Docker secret file, owner-only where the OS allows), the audit log (`audit.log`), and models (`models/`), all on one Docker volume. Nothing is stored in the cloud by this system. |
| **Who can access it?** | Anyone with the app login (`APP_PASSWORD`; one shared operator account). Without a password the app answers only this machine or a private network without a proxy. Keys are never returned by the API (only "configured" and the last four characters) and never logged. |
| **How long is it kept?** | Jobs: the newest 200 (`JOB_RETENTION`). Evaluation uploads: the newest 30 (`EVAL_RETENTION`). Ingest buffer: until the next successful job. Duplicate fingerprints: 24 hours (`INGEST_DEDUP_HOURS`). Paging keys: at most `PAGE_KEY_MAX` (default 10,000). Real / Noise marks: kept, but they store only the kind, service and time window, not the message. Model versions: kept until the operator deletes them (see risks below). Audit log: rotated to `audit.log.1` at `AUDIT_LOG_MAX_BYTES` (default 1 MB); stdout follows the team's log policy. `LOG_FORMAT=json` writes one object per line. |
| **Can data be anonymised?** | Messages are cut to 300 characters. The model's text features normalise IDs and numbers before learning (`src/features.py: normalize_message`, tested by `test_normalize_strips_ids_and_numbers`), so specific IDs are not learned as words. Evaluations can use metrics only, without any alert text. |
| **Is information sent to an external AI service?** | **No.** The model runs inside the container. The only data that leaves is the paging summary (service, time, item kind, message cut to 300 characters) sent to the team's own PagerDuty / Slack / Opsgenie in Live mode. |

**Privacy risks left, and what to do:** the saved model's text vocabulary is built from message words (after normalisation), so a model file can still contain words from logs. Keep the volume access-controlled, delete old model versions that are no longer needed, and do not send logs that contain personal data to the ingest endpoint. Filtering at Alertmanager is the right place for that.

## 2. Responsible AI

| Concern | Where it shows up here | Mitigation |
|---|---|---|
| **Bias and fairness** (between services) | The log classifier only helps services that write logs: on GAIA, 82% of faults never write one, and some services almost never log (for example `dbservice1`, 0.8%). A log-only system would favour chatty services. | Metric rules treat every service the same (81–91% jump overlap on all ten services, FINAL_REPORT §10). The alert share is capped at 25% of the budget, so chatty services cannot take the slots. Evaluate my data reports results per series. |
| **Incorrect AI output** | A false item wastes attention; a miss hides a fault. | Costs are asymmetric (a miss = 10 false alarms) and tuned on past data; the queue is capped at K items; uncertain alerts go to a person; monitoring gaps are shown, not hidden. |
| **Explainability** | An engineer must see why an item is on the list. | Each alert carries its action, score and reason (for example "policy top-k", "ask a person: inside memory jump", "severity threshold"); a jump shows its rise at confirmation and the alerts that fell inside it. The model is a linear model, so its weights can be inspected. |
| **Transparency** | Users should know when the AI decided. | Each job records whether a trained model, the saved model or rules only scored it, and the model version. Evaluate my data shows a chance line so wide labels cannot flatter the result. |
| **Human oversight** | The AI should not act on its own. | Shadow mode by default; Live only after a deliberate, audited switch with a password. People mark Real / Noise, and the marks train the next model. The operator can roll back a model. |
| **Automation bias** | A hidden alert might be ignored forever. | Hidden alerts are not deleted: they stay in the queue with the reason they were hidden, and can be marked Real. |
| **Potential misuse** | Queue data could be used to rate on-call engineers or teams. | The system records what was shown, not who responded or how fast; it should not be used for performance reviews. |
| **Safety** | Wrong automation in operations can cause outages. | The system only orders and pages; it never restarts, scales or changes anything. |
| **Reliability** | A model can get worse after an update. | Promotion gate on held-out alerts, rollback, precision-from-marks monitoring, and a retrain reminder (`docs/mlops.md`, `docs/monitoring.md`). |
