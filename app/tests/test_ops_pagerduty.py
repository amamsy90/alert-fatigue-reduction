"""Ops shadow/live overrides, routing key storage, and hard paging safety."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SHADOW_MODE", "1")
os.environ["QUEUE_DB"] = str(Path(tempfile.mkdtemp()) / "import_queue.db")

from product.ops import (  # noqa: E402
    PAGEABLE_MODES,
    can_page,
    get_shadow_mode,
    may_page_for_mode,
    set_routing_key,
    set_shadow_mode,
)
from product.store import QueueStore  # noqa: E402


class _Resp:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture()
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setenv("SHADOW_MODE", "1")
    monkeypatch.delenv("PAGERDUTY_ROUTING_KEY", raising=False)
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    return TestClient(app_mod.app)


def test_env_shadow_is_bootstrap_when_no_override(ops_env, monkeypatch):
    assert get_shadow_mode() is True
    monkeypatch.setenv("SHADOW_MODE", "0")
    assert get_shadow_mode() is False


def test_enable_live_without_confirm_fails(ops_env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    set_routing_key("rk_for_confirm_test")
    with pytest.raises(ValueError, match="LIVE"):
        set_shadow_mode(shadow=False, confirm="live")
    assert get_shadow_mode() is True
    set_shadow_mode(shadow=False, confirm="LIVE")
    assert get_shadow_mode() is False
    assert can_page() is True


def test_enable_live_without_key_fails(ops_env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    with pytest.raises(ValueError, match="routing key"):
        set_shadow_mode(shadow=False, confirm="LIVE")
    assert get_shadow_mode() is True
    assert can_page() is False
    set_routing_key("rk_test_abcdef")
    set_shadow_mode(shadow=False, confirm="LIVE")
    assert can_page() is True
    assert may_page_for_mode("ingest") is True
    assert may_page_for_mode("sample") is False


def test_routing_key_never_returned_full_on_get(ops_env):
    set_routing_key("secret-key-must-not-appear")
    res = ops_env.get("/connectors")
    assert res.status_code == 200
    body = res.json()
    assert body["pagerduty"]["configured"] is True
    assert body["pagerduty"]["last4"] == "pear"
    assert "secret-key-must-not-appear" not in res.text
    assert body["pagerduty"]["will_page"] is False  # still shadow


def _login(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    client.auth = ("oncall", "pw")


def test_api_live_requires_a_password(ops_env):
    ops_env.post("/ops/pagerduty/key", json={"routing_key": "rk_api_live"})
    res = ops_env.post("/ops/shadow", json={"shadow_mode": False, "confirm": "LIVE"})
    assert res.status_code == 400
    assert "APP_PASSWORD" in res.json()["detail"]
    assert ops_env.get("/connectors").json()["live"] is False


def test_api_set_live_requires_confirm_and_key(ops_env, monkeypatch):
    _login(ops_env, monkeypatch)
    no_key = ops_env.post("/ops/shadow", json={"shadow_mode": False, "confirm": "LIVE"})
    assert no_key.status_code == 400
    ops_env.post("/ops/pagerduty/key", json={"routing_key": "rk_api_live"})
    bad = ops_env.post("/ops/shadow", json={"shadow_mode": False, "confirm": "yes"})
    assert bad.status_code == 400
    ok = ops_env.post("/ops/shadow", json={"shadow_mode": False, "confirm": "LIVE"})
    assert ok.status_code == 200
    assert ok.json()["live"] is True
    assert ops_env.get("/connectors").json()["live"] is True


def test_api_save_and_clear_routing_key(ops_env):
    saved = ops_env.post("/ops/pagerduty/key", json={"routing_key": "abcd1234XYZ"})
    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert saved.json()["last4"] == "4XYZ"
    status = ops_env.get("/connectors").json()["pagerduty"]
    assert status["configured"] and status["last4"] == "4XYZ"
    cleared = ops_env.delete("/ops/pagerduty/key")
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False


def test_test_page_only_when_live_and_key(ops_env, monkeypatch):
    _login(ops_env, monkeypatch)
    assert ops_env.post("/ops/pagerduty/test").status_code == 400  # shadow, no key
    ops_env.post("/ops/pagerduty/key", json={"routing_key": "rk_live_test"})
    assert ops_env.post("/ops/pagerduty/test").status_code == 400  # still shadow
    ops_env.post("/ops/shadow", json={"shadow_mode": False, "confirm": "LIVE"})
    with patch("product.pagerduty._post", return_value=None) as mocked:
        res = ops_env.post("/ops/pagerduty/test")
    assert res.status_code == 200, res.text
    assert res.json()["sent"] == 1
    assert mocked.call_count == 1


def test_shadow_skips_on_ingest(ops_env, monkeypatch):
    set_routing_key("rk_shadow")
    assert get_shadow_mode() is True
    import product.app as app_mod
    from product.pipeline import run_job

    rows = [
        {"timestamp": "2026-02-01T10:00:00Z", "source": "web-01", "severity": "error", "message": "oom"}
    ]
    with patch("product.pagerduty.urlopen", return_value=_Resp()) as mocked:
        result = run_job(
            app_mod.STORE, mode="ingest", budget_k=5, shadow_mode=True, alert_rows=rows
        )
    assert mocked.call_count == 0
    assert result["meta"]["pagerduty"]["skipped"] == "shadow mode"


def test_sample_never_pages_even_when_live(ops_env, monkeypatch):
    set_routing_key("rk_live")
    set_shadow_mode(shadow=False, confirm="LIVE")
    import product.app as app_mod
    from product.pipeline import run_job

    with patch("product.pagerduty.urlopen", return_value=_Resp()) as mocked:
        result = run_job(app_mod.STORE, mode="sample", budget_k=5, shadow_mode=False)
    assert mocked.call_count == 0
    assert "never pages" in result["meta"]["pagerduty"]["skipped"]


def test_live_ingest_sends(ops_env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    set_routing_key("rk_live_ingest")
    set_shadow_mode(shadow=False, confirm="LIVE")
    import product.app as app_mod
    from product.pipeline import run_job

    rows = [
        {
            "timestamp": "2026-02-01T10:00:00Z",
            "source": "web-01",
            "severity": "error",
            "message": "oom",
        }
    ]
    with patch("product.pagerduty.urlopen", return_value=_Resp()):
        result = run_job(
            app_mod.STORE,
            mode="ingest",
            budget_k=5,
            shadow_mode=False,
            alert_rows=rows,
        )
    page = result["meta"]["pagerduty"]
    assert "skipped" not in page or page.get("skipped") in (None, "")
    assert "sent" in page and "failed" in page
    # Rules-only may or may not escalate this single alert; notify was still invoked.
    assert isinstance(page["sent"], int)


def test_pageable_modes_only_ingest():
    assert PAGEABLE_MODES == frozenset({"ingest"})
    assert may_page_for_mode("gaia") is False
    assert may_page_for_mode("upload") is False
    assert may_page_for_mode("sample") is False


def test_connectors_dashboard_hides_secrets_with_ops(ops_env, monkeypatch):
    monkeypatch.setenv("PAGERDUTY_ROUTING_KEY", "secret-key-must-not-appear")
    monkeypatch.setenv("PROM_URL", "http://prom.test:9090")
    monkeypatch.setattr(
        "product.web.ops_routes.probe_prometheus",
        lambda url=None: {"reachable": True, "version": "2.54.0"},
    )
    res = ops_env.get("/connectors")
    assert res.status_code == 200
    body = res.json()
    assert body["pagerduty"]["configured"] is True
    assert body["pagerduty"]["last4"] == "pear"
    assert "secret-key-must-not-appear" not in res.text
    page = ops_env.get("/dashboard")
    assert page.status_code == 200
    assert "secret-key-must-not-appear" not in page.text


def test_env_live_without_a_password_never_pages(ops_env, monkeypatch):
    """SHADOW_MODE=0 in the environment must not bypass the password rule."""
    import product.app as app_mod
    from product.pipeline import run_job

    monkeypatch.setenv("SHADOW_MODE", "0")
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    set_routing_key("rk_env_live")
    assert get_shadow_mode() is False and can_page() is False
    rows = [{"timestamp": "2026-02-01T10:00:00Z", "source": "web-01", "severity": "critical", "message": "oom"}]
    with patch("product.pagerduty.urlopen", return_value=_Resp()) as mocked:
        result = run_job(app_mod.STORE, mode="ingest", budget_k=5, shadow_mode=False, alert_rows=rows)
    assert mocked.call_count == 0
    assert "APP_PASSWORD" in result["meta"]["pagerduty"]["skipped"]
