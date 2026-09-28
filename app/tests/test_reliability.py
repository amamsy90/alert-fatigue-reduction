"""ops.json under concurrent writers, per-channel page marks, and SQLite connections."""
from __future__ import annotations

import sqlite3
import threading

import pandas as pd

from product import ops
from product.store import QueueStore


def test_concurrent_ops_writes_keep_every_change(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    t0 = pd.Timestamp("2026-01-01", tz="UTC")

    def keys(i: int):
        for j in range(20):
            ops.set_routing_key(f"rk-{i}-{j}")

    def marks():
        for j in range(20):
            ops.set_page_watermark(t0 + pd.Timedelta(minutes=j))

    def channels():
        for j in range(20):
            ops.set_channel_watermark("slack", t0 + pd.Timedelta(minutes=j))

    threads = [threading.Thread(target=f) for f in (lambda: keys(1), marks, channels)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ops.get_routing_key() == "rk-1-19"
    assert ops.get_page_watermark() == t0 + pd.Timedelta(minutes=19)
    assert ops.channel_cutoff("slack") == t0 + pd.Timedelta(minutes=19)
    assert not list(tmp_path.glob("*.tmp"))


def test_channel_mark_only_moves_the_channel_that_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    t0 = pd.Timestamp("2026-01-01 10:00", tz="UTC")
    ops.set_page_watermark(t0)
    # Slack delivered at 10:05; PagerDuty failed, so the shared mark stays at 10:00.
    ops.set_channel_watermark("slack", t0 + pd.Timedelta(minutes=5))
    assert ops.channel_cutoff("slack") == t0 + pd.Timedelta(minutes=5)
    assert ops.channel_cutoff("pagerduty") == t0
    # Once the shared mark passes a channel's own mark, the shared one wins.
    ops.set_page_watermark(t0 + pd.Timedelta(minutes=9))
    assert ops.channel_cutoff("slack") == t0 + pd.Timedelta(minutes=9)
    ops.clear_page_watermark()
    assert ops.get_page_watermark() is None


def test_store_closes_connections_and_uses_wal(tmp_path, monkeypatch):
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr("product.store.sqlite3.connect", spy)
    store = QueueStore(tmp_path / "q.db")
    store.buffer_alerts([{"timestamp": "2026-01-01T00:00:00Z", "source": "s", "message": "m"}])
    store.buffer_stats()
    assert opened
    for conn in opened:
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue  # closed, as it should be
        raise AssertionError("a store connection was left open")
    with real_connect(tmp_path / "q.db") as check:
        assert check.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
