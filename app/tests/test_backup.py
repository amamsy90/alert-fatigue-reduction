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
