"""Sign-in sessions, the login lock and ingest rate counters, in SQLite.

They lived in process memory, so a restart signed everyone out and cleared a
login lock an attacker had earned. The file sits beside ops.json (`AUTH_DB`
moves it). Session tokens are stored as SHA-256, so a copy of the file holds no
usable cookie. Times are wall-clock seconds, which mean the same after a restart.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_WRITE = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    role TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    kind TEXT NOT NULL,
    client TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind_client_at ON events (kind, client, at);
CREATE TABLE IF NOT EXISTS locks (
    client TEXT PRIMARY KEY,
    until REAL NOT NULL
);
"""


def auth_db_path() -> Path:
    raw = (os.environ.get("AUTH_DB") or "").strip()
    if raw:
        return Path(raw)
    from product.ops import ops_path

    return ops_path().with_name("auth.db")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """One transaction: commit on success, roll back on error, always close."""
    path = auth_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()
    if new:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def add_session(token: str, user: str, role: str, expires: float, now: float, max_sessions: int) -> None:
    """Store a new session; expired ones go, and the oldest go past `max_sessions`."""
    with _WRITE, _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
        (count,) = conn.execute("SELECT count(*) FROM sessions").fetchone()
        excess = count - max(0, max_sessions - 1)
        if excess > 0:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash IN "
                "(SELECT token_hash FROM sessions ORDER BY expires LIMIT ?)",
                (excess,),
            )
        conn.execute(
            "INSERT OR REPLACE INTO sessions (token_hash, user, role, expires) VALUES (?, ?, ?, ?)",
            (_hash(token), user, role, expires),
        )


def get_session(token: str, now: float) -> tuple[str, str] | None:
    """(user, role) for a live session; an expired one is deleted."""
    key = _hash(token)
    with _connect() as conn:
        row = conn.execute("SELECT user, role, expires FROM sessions WHERE token_hash = ?", (key,)).fetchone()
        if row is None:
            return None
        if row[2] <= now:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (key,))
            return None
        return row[0], row[1]


def drop_session(token: str) -> None:
    with _WRITE, _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


def lock_wait(client: str, now: float) -> float:
    """Seconds left on this client's login lock, or 0."""
    with _connect() as conn:
        row = conn.execute("SELECT until FROM locks WHERE client = ?", (client,)).fetchone()
        if row is None:
            return 0.0
        if row[0] <= now:
            conn.execute("DELETE FROM locks WHERE client = ?", (client,))
            return 0.0
        return float(row[0] - now)


def note_login_failure(client: str, now: float, max_fails: int, seconds: float) -> float:
    """Record one bad password. Returns the lock length when this attempt starts one."""
    with _WRITE, _connect() as conn:
        conn.execute("DELETE FROM events WHERE kind = 'login_fail' AND at <= ?", (now - seconds,))
        (recent,) = conn.execute(
            "SELECT count(*) FROM events WHERE kind = 'login_fail' AND client = ?", (client,)
        ).fetchone()
        if recent + 1 >= max_fails:
            conn.execute("DELETE FROM events WHERE kind = 'login_fail' AND client = ?", (client,))
            conn.execute("INSERT OR REPLACE INTO locks (client, until) VALUES (?, ?)", (client, now + seconds))
            return seconds
        conn.execute("INSERT INTO events (kind, client, at) VALUES ('login_fail', ?, ?)", (client, now))
        return 0.0


def clear_login_failures(client: str) -> None:
    with _WRITE, _connect() as conn:
        conn.execute("DELETE FROM events WHERE kind = 'login_fail' AND client = ?", (client,))
        conn.execute("DELETE FROM locks WHERE client = ?", (client,))


def note_ingest_batch(client: str, now: float, max_batches: int, window: float) -> float:
    """Record one ingest POST. Returns the window length when this client is over the cap."""
    with _WRITE, _connect() as conn:
        conn.execute("DELETE FROM events WHERE kind = 'ingest' AND at <= ?", (now - window,))
        (recent,) = conn.execute(
            "SELECT count(*) FROM events WHERE kind = 'ingest' AND client = ?", (client,)
        ).fetchone()
        if recent >= max_batches:
            return window
        conn.execute("INSERT INTO events (kind, client, at) VALUES ('ingest', ?, ?)", (client, now))
        return 0.0


def reset_all() -> None:
    """Empty every table. Tests use this."""
    with _WRITE, _connect() as conn:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM locks")
