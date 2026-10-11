"""Backup copies runtime files and restore puts them back."""
from __future__ import annotations

from product.backup import backup, restore
from product.store import QueueStore
from src.entities import Action, Alert, Decision


def test_backup_and_restore_bring_the_queue_back(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    db = live / "queue.db"
    monkeypatch.setenv("QUEUE_DB", str(db))
    monkeypatch.setenv("OPS_PATH", str(live / "ops.json"))
    monkeypatch.setenv("SETTINGS_PATH", str(live / "settings.json"))
    monkeypatch.setenv("MODEL_PATH", str(live / "model.pkl"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(live / "audit.log"))
    (live / "ops.json").write_text('{"shadow_mode": true}\n', encoding="utf-8")
    (live / "audit.log").write_text("audit line\n", encoding="utf-8")
    store = QueueStore(db)
    t = "2026-02-01T10:00:00Z"
    import pandas as pd

    alerts = [Alert("a0", pd.Timestamp(t), "web", "log", "info", "hello")]
    job_id = store.save_job(
        source="sample",
        budget_k=20,
        escalate_threshold=1.0,
        review_threshold=1.0,
        shadow_mode=True,
        alerts=alerts,
        decisions=[Decision(alerts[0].alert_id, Action.SUPPRESS, 0.1, "")],
        spike_flags=[False],
        meta={"alert_count": 1},
    )
    dest = tmp_path / "copy"
    copied = backup(dest)
    assert "queue.db" in copied
    assert "ops.json" in copied
    assert "audit.log" in copied
    db.unlink()
    (live / "ops.json").unlink()
    restored = restore(dest)
    assert "queue.db" in restored
    again = QueueStore(db)
    assert again.latest_job_id() == job_id
    assert '"shadow_mode": true' in (live / "ops.json").read_text(encoding="utf-8")


def test_backup_includes_commits_still_in_the_write_ahead_log(tmp_path, monkeypatch):
    """Requirement: a backup taken while the app runs holds every committed job."""
    import sqlite3

    live = tmp_path / "live"
    live.mkdir()
    db = live / "queue.db"
    monkeypatch.setenv("QUEUE_DB", str(db))
    monkeypatch.setenv("OPS_PATH", str(live / "ops.json"))
    monkeypatch.setenv("MODEL_PATH", str(live / "model.pkl"))
    monkeypatch.setenv("AUDIT_LOG_PATH", "off")
    QueueStore(db)
    # A reader held open (the queue page) keeps the commit below in the -wal file.
    reader = sqlite3.connect(db)
    reader.execute("SELECT count(*) FROM jobs").fetchall()
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(
        "INSERT INTO jobs (job_id, created_at, source, budget_k, escalate_threshold, review_threshold) "
        "VALUES ('j1', '2026-02-01T10:00:00Z', 'sample', 20, 1.0, 1.0)"
    )
    writer.commit()
    try:
        assert (live / "queue.db-wal").stat().st_size > 0
        backup(tmp_path / "copy")
    finally:
        writer.close()
        reader.close()
    copied = sqlite3.connect(tmp_path / "copy" / "queue.db")
    try:
        assert copied.execute("SELECT job_id FROM jobs").fetchall() == [("j1",)]
    finally:
        copied.close()


def test_restore_drops_the_stale_write_ahead_log_of_the_lost_database(tmp_path, monkeypatch):
    """Requirement: after a restore the queue holds what the backup holds, not
    commits replayed from the crashed database's log."""
    import shutil
    import sqlite3

    live = tmp_path / "live"
    live.mkdir()
    db = live / "queue.db"
    monkeypatch.setenv("QUEUE_DB", str(db))
    monkeypatch.setenv("OPS_PATH", str(live / "ops.json"))
    monkeypatch.setenv("MODEL_PATH", str(live / "model.pkl"))
    monkeypatch.setenv("AUDIT_LOG_PATH", "off")
    QueueStore(db)
    backup(tmp_path / "copy")
    # The crash: a commit is left in -wal, and the files are copied while open.
    reader = sqlite3.connect(db)
    reader.execute("SELECT count(*) FROM jobs").fetchall()
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(
        "INSERT INTO jobs (job_id, created_at, source, budget_k, escalate_threshold, review_threshold) "
        "VALUES ('lost', '2026-02-01T10:00:00Z', 'sample', 20, 1.0, 1.0)"
    )
    writer.commit()
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    for suffix in ("", "-wal", "-shm"):
        shutil.copy2(live / f"queue.db{suffix}", crashed / f"queue.db{suffix}")
    writer.close()
    reader.close()
    for suffix in ("", "-wal", "-shm"):
        (live / f"queue.db{suffix}").unlink(missing_ok=True)
        shutil.copy2(crashed / f"queue.db{suffix}", live / f"queue.db{suffix}")
    restore(tmp_path / "copy")
    assert QueueStore(db).latest_job_id() is None
