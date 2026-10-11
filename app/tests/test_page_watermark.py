"""Live paging only covers metric items that are new since the last ingest job."""
from __future__ import annotations

import pandas as pd
import pytest

from product import ops
from product.pagerduty import would_have_paged
from src.memory_spikes import Spike


@pytest.fixture
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    monkeypatch.delenv("PAGE_LOOKBACK_MINUTES", raising=False)
    return tmp_path


def _spike(minutes_ago: float, now: pd.Timestamp) -> Spike:
    t = now - pd.Timedelta(minutes=minutes_ago)
    return Spike("s", "svc", t, t + pd.Timedelta(minutes=5), 1e9, t, 1e9)


def test_first_cutoff_is_lookback(ops_env):
    now = pd.Timestamp("2026-09-27 12:00", tz="UTC")
    assert ops.page_cutoff(now) == now - pd.Timedelta(minutes=60)


def test_watermark_replaces_lookback(ops_env):
    now = pd.Timestamp("2026-09-27 12:00", tz="UTC")
    mark = now - pd.Timedelta(minutes=5)
    ops.set_page_watermark(mark)
    assert ops.page_cutoff(now) == mark
    ops.clear_page_watermark()
    assert ops.get_page_watermark() is None


def test_old_history_is_not_paged(ops_env):
    now = pd.Timestamp("2026-09-27 12:00", tz="UTC")
    history = [_spike(m, now) for m in (60 * 24 * 7, 60 * 24, 120, 30, 2)]
    cutoff = ops.page_cutoff(now)
    new = [s for s in history if cutoff < s.shown_at <= now]
    assert len(new) == 2
    assert would_have_paged([], [], spikes=new)["spikes"] == 2


def test_redact_url_hides_credentials():
    assert ops.redact_url("http://user:pw@prom:9090") == "http://***@prom:9090"
    assert ops.redact_url("http://prom:9090") == "http://prom:9090"


def _history(now: pd.Timestamp) -> pd.DataFrame:
    """Two days of 30 s memory samples with a 1 GB jump 1 day ago and one 10 min ago."""
    ts = pd.date_range(now - pd.Timedelta(days=2), now, freq="30s")
    base = pd.Series(2e9, index=ts)
    for ago in (pd.Timedelta(days=1), pd.Timedelta(minutes=10)):
        start = now - ago
        base[(ts >= start) & (ts < start + pd.Timedelta(minutes=5))] += 1e9
    return pd.DataFrame({"timestamp": ts, "value": base.values, "service": "web", "series_id": "web"})


def test_ingest_job_pages_only_new_jumps(ops_env, monkeypatch):
    import product.pipeline as pipeline
    from product.store import QueueStore

    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH"):
        monkeypatch.setenv(name, str(ops_env / f"{name.lower()}.x"))
    ops.set_prom_url("http://93.184.216.34:9090")
    now = pd.Timestamp.now(tz="UTC").floor("30s")
    monkeypatch.setattr("product.prometheus_adapter.fetch_memory_metrics", lambda *a, **k: _history(now))
    store = QueueStore(ops_env / "queue.db")
    rows = [{"timestamp": now.isoformat(), "source": "web", "severity": "info", "message": "hello"}]

    first = pipeline.run_job(store, mode="ingest", budget_k=20, shadow_mode=True, alert_rows=rows)
    whp = first["meta"]["would_have_paged"]
    assert whp["spikes"] == 1  # the jump 10 min ago, not the one a day ago
    assert ops.get_page_watermark() is not None
    assert ops.seen_spike_keys()

    second = pipeline.run_job(store, mode="ingest", budget_k=20, shadow_mode=True, alert_rows=rows)
    assert second["meta"]["would_have_paged"]["spikes"] == 0  # already considered


def test_late_point_is_paged_even_when_its_timestamp_is_behind_the_clock(ops_env):
    """A point that reaches Prometheus after the previous job still has a new key."""
    from product.paging import page_job

    now = pd.Timestamp("2026-09-27 12:00", tz="UTC")
    old = _spike(60 * 24, now)
    recent = _spike(10, now)
    _, first = page_job("ingest", True, [], [], [old, recent], run_at=now)
    assert first["spikes"] == 1

    late = _spike(30, now)  # confirmed before this next job's clock, but not in the first pull
    _, second = page_job(
        "ingest", True, [], [], [old, recent, late], run_at=now + pd.Timedelta(minutes=5)
    )
    assert second["spikes"] == 1
    assert second["items"][0]["dedup_key"].startswith("jump:svc:")


def test_failed_channel_retries_only_that_channel(ops_env, monkeypatch):
    from product.paging import page_job

    monkeypatch.setenv("APP_PASSWORD", "pw")
    ops.set_routing_key("rk")
    ops.set_slack_webhook("https://hooks.slack.com/x")
    ops.set_shadow_mode(shadow=False, confirm="LIVE")
    now = pd.Timestamp("2026-09-27 12:00", tz="UTC")
    recent = _spike(10, now)
    calls = {"pagerduty": 0, "slack": 0}

    def pd_fail(*_a, **_k):
        calls["pagerduty"] += 1
        return {"sent": 0, "failed": 1}

    def slack_ok(items, count, **_k):
        calls["slack"] += 1
        return {"sent": 1, "failed": 0}

    monkeypatch.setattr("product.paging.notify_escalations", pd_fail)
    monkeypatch.setattr("product.paging.notify_slack", slack_ok)
    page_job("ingest", False, [], [], [recent], run_at=now)
    assert calls == {"pagerduty": 1, "slack": 1}
    calls["pagerduty"] = calls["slack"] = 0
    page_job("ingest", False, [], [], [recent], run_at=now + pd.Timedelta(minutes=2))
    assert calls == {"pagerduty": 1, "slack": 0}


def test_failed_slack_is_resent_and_pagerduty_is_not(ops_env, monkeypatch):
    from product.paging import page_job

    monkeypatch.setenv("APP_PASSWORD", "pw")
    ops.set_routing_key("rk")
    ops.set_slack_webhook("https://hooks.slack.com/x")
    ops.set_shadow_mode(shadow=False, confirm="LIVE")
    now = pd.Timestamp("2026-09-27 12:00", tz="UTC")
    recent = _spike(10, now)
    calls = {"pagerduty": 0, "slack": 0}

    def pd_ok(*_a, **_k):
        calls["pagerduty"] += 1
        return {"sent": 1, "failed": 0}

    def slack_fail(items, count, **_k):
        calls["slack"] += 1
        return {"sent": 0, "failed": 1}

    monkeypatch.setattr("product.paging.notify_escalations", pd_ok)
    monkeypatch.setattr("product.paging.notify_slack", slack_fail)
    page_job("ingest", False, [], [], [recent], run_at=now)
    assert calls == {"pagerduty": 1, "slack": 1}
    calls["pagerduty"] = calls["slack"] = 0
    page_job("ingest", False, [], [], [recent], run_at=now + pd.Timedelta(minutes=2))
    assert calls == {"pagerduty": 0, "slack": 1}


def test_paged_item_keys_are_capped(ops_env, monkeypatch):
    monkeypatch.setenv("PAGE_KEY_MAX", "3")
    ops.set_seen_spike_keys({f"k{i}" for i in range(5)})
    kept = ops.seen_spike_keys()
    assert kept == {"k2", "k3", "k4"}
    ops.add_channel_delivered_keys("slack", {f"s{i}" for i in range(5)})
    assert ops.channel_delivered_keys("slack") == {"s2", "s3", "s4"}


def test_an_item_still_in_history_is_never_paged_twice_once_the_key_cap_is_reached(ops_env, monkeypatch):
    """Requirement: an item already considered for paging is not paged again while
    the re-scored history still shows it, however many keys have been stored."""
    from product.paging import page_job

    monkeypatch.setenv("PAGE_KEY_MAX", "4")
    now = pd.Timestamp("2026-09-27 12:00", tz="UTC")

    def item(kind: str, service: str, minutes_ago: float) -> Spike:
        t = now - pd.Timedelta(minutes=minutes_ago)
        return Spike(service, service, t, t + pd.Timedelta(minutes=5), 1e9, t, 1e9, kind=kind)

    # Months of items that have left the history, then the current history:
    # gaps sort before jumps by name, so an alphabetical cap would drop them first.
    old = [item("jump", f"z{i}", 60 * 24 * 200 + i) for i in range(6)]
    current = [item("gap", "a", 30), item("gap", "b", 20), item("jump", "c", 10)]
    page_job("ingest", True, [], [], old + current, run_at=now)

    _, again = page_job("ingest", True, [], [], current, run_at=now + pd.Timedelta(minutes=1))
    assert again["spikes"] == 0
    stored = ops.seen_spike_keys() or set()
    assert len(stored) <= 4


def test_key_cap_drops_the_oldest_keys_first(ops_env, monkeypatch):
    monkeypatch.setenv("PAGE_KEY_MAX", "2")
    t = pd.Timestamp("2026-09-27 12:00", tz="UTC")
    keys = {f"gap:a:{(t - pd.Timedelta(days=d)).isoformat()}" for d in (1, 2, 3)}
    ops.set_seen_spike_keys(keys)
    assert ops.seen_spike_keys() == {f"gap:a:{(t - pd.Timedelta(days=d)).isoformat()}" for d in (1, 2)}
