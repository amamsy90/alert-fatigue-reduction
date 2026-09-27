"""Slack and Opsgenie paging share PagerDuty's gate and item list."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SHADOW_MODE", "1")
os.environ.setdefault("QUEUE_DB", str(Path(tempfile.mkdtemp()) / "notif.db"))

from product import notifiers, ops  # noqa: E402
from product.pagerduty import page_items  # noqa: E402
from product.store import QueueStore  # noqa: E402
from src.entities import Action, Alert, Decision  # noqa: E402
from src.memory_spikes import Spike  # noqa: E402


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setenv("SHADOW_MODE", "1")
    for var in ("PAGERDUTY_ROUTING_KEY", "SLACK_WEBHOOK_URL", "OPSGENIE_API_KEY", "OPSGENIE_API_URL"):
        monkeypatch.delenv(var, raising=False)
    for var in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH"):
        monkeypatch.setenv(var, str(tmp_path / f"{var.lower()}.x"))
    import product.app as app_mod

    app_mod.STORE = QueueStore(tmp_path / "queue.db")
    return TestClient(app_mod.app)


def _items():
    t = pd.Timestamp("2026-09-27 10:00", tz="UTC")
    jump = Spike("web", "web", t, t + pd.Timedelta(minutes=5), 1e9, t, 1e9)
    gap = Spike("db", "db", t, t, kind="gap")
    alert = Alert("a1", t, "api", "HighErr", "critical", "5xx rate high")
    return page_items([alert], [Decision("a1", Action.ESCALATE, 0.9, "r")], spikes=[jump, gap])


def test_page_items_are_the_shared_list():
    items = _items()
    assert [i["kind"] for i in items] == ["jump", "gap", "alert"]
    assert items[2]["severity"] == "critical" and items[1]["severity"] == "warning"


def test_slack_one_message_per_job_capped():
    many = [{"kind": "jump", "service": f"s{i}", "summary": "x", "dedup_key": f"k{i}"} for i in range(30)]
    body = notifiers.slack_message(many, 30)
    assert body["text"].count("•") == notifiers.MAX_SLACK_LINES
    assert "and 10 more" in body["text"]
    with patch("product.notifiers.urlopen", return_value=_Resp()) as post:
        r = notifiers.notify_slack(many, 30, url="https://hooks.slack.com/x")
    assert r == {"sent": 1, "failed": 0, "items": 30} and post.call_count == 1
    assert notifiers.notify_slack([], 0, url="https://hooks.slack.com/x")["skipped"] == "nothing new"


def test_opsgenie_one_alert_per_item_with_alias_and_priority():
    seen = []

    def fake(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        return _Resp()

    with patch("product.notifiers.urlopen", side_effect=fake):
        r = notifiers.notify_opsgenie(_items(), key="og-secret", api_url="https://api.eu.opsgenie.com")
    assert r["sent"] == 3 and r["failed"] == 0
    assert seen[0][0] == "https://api.eu.opsgenie.com/v2/alerts"
    assert seen[0][1] == "GenieKey og-secret"
    assert seen[0][2]["alias"].startswith("jump:web:") and seen[0][2]["priority"] == "P2"
    assert seen[2][2]["priority"] == "P1"  # critical alert


def test_failures_never_echo_the_secret():
    with patch("product.notifiers.urlopen", side_effect=RuntimeError("https://hooks.slack.com/secret")):
        r = notifiers.notify_slack(_items(), 3, url="https://hooks.slack.com/secret")
    assert r["failed"] == 1 and "secret" not in json.dumps(r)


def test_live_needs_any_channel(env):
    with pytest.raises(ValueError, match="channel"):
        ops.set_shadow_mode(shadow=False, confirm="LIVE")
    ops.set_slack_webhook("https://hooks.slack.com/x")
    assert ops.configured_channels() == ["slack"]
    ops.set_shadow_mode(shadow=False, confirm="LIVE")
    assert ops.can_page()
    with pytest.raises(ValueError, match="hooks.slack.com"):
        ops.set_slack_webhook("http://plain")


def test_api_saves_and_reports_without_the_secret(env):
    r = env.post("/ops/opsgenie/key", json={"api_key": "abcd-1234"})
    assert r.status_code == 200 and r.json()["last4"] == "1234"
    c = env.get("/connectors").json()
    assert c["opsgenie"]["configured"] and "abcd" not in json.dumps(c)
    assert c["channels"] == ["opsgenie"]
    assert env.post("/ops/opsgenie/test").status_code == 400  # shadow
    assert env.delete("/ops/opsgenie/key").json()["configured"] is False


def test_live_ingest_fans_out_to_every_channel(env, monkeypatch):
    import product.app as app_mod
    from product.pipeline import run_job

    ops.set_routing_key("rk")
    ops.set_slack_webhook("https://hooks.slack.com/x")
    ops.set_opsgenie_key("og")
    ops.set_shadow_mode(shadow=False, confirm="LIVE")
    rows = [{"timestamp": pd.Timestamp.now(tz="UTC").isoformat(), "source": "web",
             "severity": "critical", "message": "oom"}]
    with patch("product.pagerduty.urlopen", return_value=_Resp()), \
         patch("product.notifiers.urlopen", return_value=_Resp()):
        result = run_job(app_mod.STORE, mode="ingest", budget_k=5, shadow_mode=False, alert_rows=rows)
    page = result["meta"]["pagerduty"]
    assert set(page["channels"]) == {"pagerduty", "slack", "opsgenie"}
    assert page["failed"] == 0
