---
name: run-the-live-soak
description: Run the real app, Prometheus and Alertmanager in Docker around a simulated fleet with injected faults, and record what the loop did.
---

# Run the live soak

Needs Docker. Nothing can page: shadow mode is on and no channel is configured.

1. From `app/`: `uv run python run_live_soak.py --minutes 120 --warmup 900 --fault-every 300 --job-every 180 --retrain-every 600`. A 15-minute try: `--minutes 15 --warmup 300 --fault-every 120 --job-every 60`.
2. Drift and retrain phase (posts alert storms made by the script, because a soak sends too few alerts for the drift check): `uv run python run_live_soak.py --minutes 0 --drift-phase --out results/live_drift_phase.json`.
3. Results go to `app/results/live_soak.json`. Read: faults judged and caught, false jumps, median delay, jobs run/skipped/failed, marks, retrains, monitoring status, and whether Prometheus scraped the app's own `/metrics`.
4. Faults on one service must be at least 15 minutes apart (the jump rule compares with the median of the previous 15 minutes). The fleet enforces 20; a test guards it.
5. A job is skipped, not failed, when no new alert arrived since the last one.
6. Report it as a soak test. The services, the faults and the engineer are simulated; it is not production traffic and not a pilot.
7. If a number goes into the report, add it to `run_validate.py` and the checked-numbers table (see `reproduce-and-validate`).
