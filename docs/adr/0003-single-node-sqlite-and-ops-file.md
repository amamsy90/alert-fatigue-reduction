# 0003 — One process with SQLite and a locked JSON file

**Status:** Accepted

## Context
The target user is one team on one server, often in a pilot. A database server would add installation and operations work that such a team does not want. The state is small: jobs, decisions, marks, a buffer of alerts, and a few operator settings (Live switch, keys, Prometheus URL, page marks).

## Decision
- Relational state lives in SQLite (`product/store.py`). Each call is one transaction that commits or rolls back and always closes its connection. WAL mode lets the queue page read while an ingest writes; a busy timeout waits for a writer instead of failing.
- Operator settings live in `ops.json` (`product/ops.py`), changed through one locked read-modify-write and written through a unique temporary file, owner-only where the OS allows.
- One uvicorn worker. A job lock and an evaluation lock keep CPU-heavy work to one at a time; a second request gets 409.

## Consequences
- `docker compose up` is the whole install; backups are one volume.
- Sign-in sessions, the login lock and the ingest counters moved to `auth.db` (SQLite) so they survive a restart; they no longer tie the app to one process. The job, evaluation and `ops.json` locks are still in-process, so running several workers or replicas is not supported. Scaling out would need a real database and a shared lock (for example PostgreSQL advisory locks).
- Paging secrets live in `secrets.json` (or a Docker secret file), not in `ops.json`. The file is owner-only where the OS allows.
