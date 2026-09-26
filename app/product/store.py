"""SQLite store for scored alert-queue jobs."""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pandas as pd

from src.entities import Alert, Decision


DEFAULT_DB = Path(__file__).resolve().parent / "data" / "queue.db"


class QueueStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    budget_k INTEGER NOT NULL,
                    escalate_threshold REAL NOT NULL,
                    review_threshold REAL NOT NULL,
                    shadow_mode INTEGER NOT NULL DEFAULT 1,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    alert_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    day TEXT NOT NULL,
                    source TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    action TEXT NOT NULL,
                    score REAL NOT NULL,
                    reason TEXT NOT NULL,
                    during_memory_spike INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_job_day
                    ON decisions(job_id, day, action);
                CREATE TABLE IF NOT EXISTS jumps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    service TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    rise_gb REAL NOT NULL,
                    alerts_inside INTEGER NOT NULL,
                    escalated_inside INTEGER NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_jumps_job_day ON jumps(job_id, day);
                CREATE TABLE IF NOT EXISTS feedback (
                    kind TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    service TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (kind, item_id)
                );
                CREATE TABLE IF NOT EXISTS ingest_buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            # Columns added after the first release: add them to old databases.
            have = {r["name"] for r in conn.execute("PRAGMA table_info(jumps)")}
            for name, ddl in (
                ("kind", "TEXT NOT NULL DEFAULT 'jump'"),
                ("confirm", "TEXT"),
                ("shown", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if name not in have:
                    conn.execute(f"ALTER TABLE jumps ADD COLUMN {name} {ddl}")
            have = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
            if "alert_id" not in have:
                conn.execute("ALTER TABLE feedback ADD COLUMN alert_id TEXT")

    def latest_job_id(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else str(row["job_id"])

    def save_job(
        self,
        *,
        source: str,
        budget_k: int,
        escalate_threshold: float,
        review_threshold: float,
        shadow_mode: bool,
        alerts: list[Alert],
        decisions: list[Decision],
        spike_flags: list[bool],
        jumps: list[dict] | None = None,
        meta: dict | None = None,
        day_offset_hours: float = 0.0,
    ) -> str:
        """`decisions` and `spike_flags` are aligned with `alerts` by position."""
        if not (len(alerts) == len(decisions) == len(spike_flags)):
            raise ValueError("alerts, decisions and spike_flags must be the same length")
        job_id = uuid.uuid4().hex[:12]
        created = pd_utcnow()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, created_at, source, budget_k,
                    escalate_threshold, review_threshold, shadow_mode, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    created,
                    source,
                    budget_k,
                    escalate_threshold,
                    review_threshold,
                    1 if shadow_mode else 0,
                    json.dumps(meta or {}),
                ),
            )
            rows = []
            for alert, decision, in_spike in zip(alerts, decisions, spike_flags):
                ts = alert.timestamp.isoformat()
                day = (alert.timestamp + pd.Timedelta(hours=day_offset_hours)).normalize().date().isoformat()
                rows.append(
                    (
                        job_id,
                        alert.alert_id,
                        ts,
                        day,
                        alert.source,
                        alert.severity,
                        alert.message[:300],
                        decision.action.value,
                        float(decision.confidence),
                        decision.reason,
                        1 if in_spike else 0,
                    )
                )
            conn.executemany(
                """
                INSERT INTO decisions (
                    job_id, alert_id, timestamp, day, source, severity, message,
                    action, score, reason, during_memory_spike
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.executemany(
                """
                INSERT INTO jumps (
                    job_id, day, service, start, end, rise_gb,
                    alerts_inside, escalated_inside, kind, confirm, shown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        job_id,
                        j["day"],
                        j["service"],
                        j["start"],
                        j["end"],
                        j["rise_gb"],
                        j["alerts_inside"],
                        j["escalated_inside"],
                        j.get("kind", "jump"),
                        j.get("confirm") or j["start"],
                        1 if j.get("shown", True) else 0,
                    )
                    for j in jumps or []
                ],
            )
        return job_id

    def get_jumps(
        self, job_id: str, *, day: str | None = None, per_day: int | None = None
    ) -> tuple[list[dict], int]:
        """Jump and gap items in the order on-call saw them (first confirmed first).

        Returns (shown, total). Only items the causal queue showed are returned
        when `per_day` is given (it still caps each day, for old jobs).
        """
        clauses = ["job_id = ?"]
        params: list = [job_id]
        if day:
            clauses.append("day = ?")
            params.append(day)
        sql = f"""
            SELECT j.id, j.day, j.service, j.start, j.end, j.rise_gb,
                   j.alerts_inside, j.escalated_inside, j.kind, j.confirm, j.shown, f.verdict
            FROM jumps j
            LEFT JOIN feedback f ON f.kind = 'jump' AND f.item_id = j.id
            WHERE {" AND ".join("j." + c for c in clauses)}
            ORDER BY j.day, COALESCE(j.confirm, j.start)
        """
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if per_day is None:
            return rows, len(rows)
        shown: list[dict] = []
        taken: dict[str, int] = {}
        for row in rows:
            if not row["shown"]:
                continue
            if taken.get(row["day"], 0) < per_day:
                shown.append(row)
                taken[row["day"]] = taken.get(row["day"], 0) + 1
        return shown, len(rows)

    def list_days(self, job_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT day FROM decisions WHERE job_id = ? ORDER BY day",
                (job_id,),
            ).fetchall()
        return [str(r["day"]) for r in rows]

    def get_queue(
        self,
        job_id: str,
        *,
        day: str | None = None,
        action: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        clauses = ["job_id = ?"]
        params: list = [job_id]
        if day:
            clauses.append("day = ?")
            params.append(day)
        if action and action != "all":
            clauses.append("action = ?")
            params.append(action)
        sql = f"""
            SELECT d.id, d.alert_id, d.timestamp, d.day, d.source, d.severity, d.message,
                   d.action, d.score, d.reason, d.during_memory_spike, f.verdict
            FROM decisions d
            LEFT JOIN feedback f ON f.kind = 'alert' AND f.item_id = d.id
            WHERE {" AND ".join("d." + c for c in clauses)}
            ORDER BY
                CASE d.action
                    WHEN 'escalate' THEN 0
                    WHEN 'review' THEN 1
                    ELSE 2
                END,
                d.score DESC,
                d.timestamp ASC
            LIMIT ?
        """
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def set_feedback(self, kind: str, item_id: int, verdict: str | None) -> dict:
        """Record "real" / "noise" for one alert or jump; None clears it.

        The item's service and time window are copied so labels survive after
        the job that produced them is gone.
        """
        if kind not in ("alert", "jump"):
            raise ValueError("kind must be alert or jump")
        if verdict not in ("real", "noise", None):
            raise ValueError("verdict must be real, noise or empty")
        with self._connect() as conn:
            if kind == "jump":
                row = conn.execute(
                    "SELECT service, start, end, NULL AS alert_id FROM jumps WHERE id = ?", (item_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT source AS service, timestamp AS start, timestamp AS end, alert_id "
                    "FROM decisions WHERE id = ?",
                    (item_id,),
                ).fetchone()
            if row is None:
                raise KeyError(f"No {kind} with id {item_id}")
            if verdict is None:
                conn.execute(
                    "DELETE FROM feedback WHERE kind = ? AND item_id = ?", (kind, item_id)
                )
            else:
                conn.execute(
                    """
                    INSERT INTO feedback (kind, item_id, verdict, service, start, end, created_at, alert_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (kind, item_id) DO UPDATE SET
                        verdict = excluded.verdict, created_at = excluded.created_at
                    """,
                    (kind, item_id, verdict, row["service"], row["start"], row["end"], pd_utcnow(),
                     row["alert_id"]),
                )
        return {"kind": kind, "id": item_id, "verdict": verdict}

    def feedback_faults(self, alert_pad_minutes: float = 5.0) -> list[dict]:
        """Items marked "real" as fault windows in the faults.csv shape.

        A real jump is its own window; a real alert gets +/- `alert_pad_minutes`
        so neighbouring alerts from the same incident share the label.
        """
        import pandas as pd

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, item_id, service, start, end FROM feedback "
                "WHERE verdict = 'real' ORDER BY start"
            ).fetchall()
        pad = pd.Timedelta(minutes=alert_pad_minutes)
        faults = []
        for row in rows:
            start = pd.Timestamp(row["start"])
            end = pd.Timestamp(row["end"])
            if row["kind"] == "alert":
                start, end = start - pad, end + pad
            faults.append(
                {
                    "fault_id": f"fb_{row['kind']}_{row['item_id']}",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "component": row["service"],
                    "description": f"confirmed by on-call ({row['kind']})",
                }
            )
        return faults

    def feedback_summary(self) -> dict:
        """Marks per kind, plus live precision: the share of marked items that were real."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, verdict, COUNT(*) AS n FROM feedback GROUP BY kind, verdict"
            ).fetchall()
        summary: dict = {"real": 0, "noise": 0}
        for kind in ("alert", "jump"):
            summary[kind] = {"real": 0, "noise": 0, "precision": None}
        for row in rows:
            summary[row["kind"]][row["verdict"]] = int(row["n"])
            summary[row["verdict"]] += int(row["n"])
        for kind in ("alert", "jump"):
            marked = summary[kind]["real"] + summary[kind]["noise"]
            if marked:
                summary[kind]["precision"] = round(summary[kind]["real"] / marked, 3)
        return summary

    def feedback_noise_alerts(self) -> set[tuple[str, int]]:
        """(service, time in UTC nanoseconds) of alerts a person marked as noise.

        Times are compared as instants, not as text, so a different timestamp
        format in the next export still matches. Alert ids are stored with the
        mark but not used to match: uploads without ids get positional ids
        (w000001, ...) that repeat across unrelated batches.
        """
        import pandas as pd

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT service, start FROM feedback WHERE kind = 'alert' AND verdict = 'noise'"
            ).fetchall()
        return {(str(r["service"]), pd.Timestamp(r["start"]).value) for r in rows}

    def job_meta(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["meta"] = json.loads(data.pop("meta_json") or "{}")
        data["shadow_mode"] = bool(data["shadow_mode"])
        return data

    def buffer_alerts(self, payload: list[dict]) -> int:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ingest_buffer (received_at, payload_json) VALUES (?, ?)",
                (pd_utcnow(), json.dumps(payload)),
            )
            row = conn.execute("SELECT COUNT(*) AS n FROM ingest_buffer").fetchone()
        return int(row["n"])

    def buffer_stats(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS batches, MAX(received_at) AS last_at FROM ingest_buffer"
            ).fetchone()
            jobs = conn.execute(
                "SELECT job_id, created_at, source, shadow_mode, meta_json "
                "FROM jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        last_job = None
        if jobs:
            meta = json.loads(jobs["meta_json"] or "{}")
            last_job = {
                "job_id": jobs["job_id"],
                "created_at": jobs["created_at"],
                "source": jobs["source"],
                "shadow_mode": bool(jobs["shadow_mode"]),
                "pagerduty": meta.get("pagerduty"),
                "has_metrics": meta.get("has_metrics"),
            }
        return {
            "buffered_batches": int(row["batches"] or 0),
            "last_ingest_at": row["last_at"],
            "last_job": last_job,
        }

    def peek_buffer(self) -> tuple[list[dict], int | None]:
        """Buffered alert rows and the last batch id, without deleting anything.

        Call `ack_buffer(last_id)` only after the rows were scored and saved, so a
        failed run leaves them in the buffer for the next try.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, payload_json FROM ingest_buffer ORDER BY id"
            ).fetchall()
        last_id = int(rows[-1]["id"]) if rows else None
        return self._unpack(rows), last_id

    def ack_buffer(self, last_id: int | None) -> None:
        if last_id is None:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM ingest_buffer WHERE id <= ?", (last_id,))

    def drain_buffer(self) -> list[dict]:
        """Peek and delete at once (kept for callers that cannot fail)."""
        rows, last_id = self.peek_buffer()
        self.ack_buffer(last_id)
        return rows

    @staticmethod
    def _unpack(rows) -> list[dict]:
        alerts: list[dict] = []
        for row in rows:
            chunk = json.loads(row["payload_json"])
            if isinstance(chunk, list):
                alerts.extend(chunk)
            else:
                alerts.append(chunk)
        return alerts


def pd_utcnow() -> str:
    import pandas as pd

    return pd.Timestamp.now("UTC").isoformat()
