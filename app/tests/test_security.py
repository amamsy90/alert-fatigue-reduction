"""Security gates: Prometheus SSRF check, login behind proxies, Host and CSRF checks, audit log."""
from __future__ import annotations

import logging
import socket
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from product import ops
from product.store import QueueStore


@pytest.fixture()
def private_off(monkeypatch):
    monkeypatch.delenv("PROM_ALLOW_PRIVATE", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1:9090",
        "http://2130706433:9090",
        "http://0x7f000001:9090",
        "http://0177.0.0.1:9090",
        "http://localhost.:9090",
        "http://app.localhost:9090",
        "http://[::1]:9090",
        "http://[::ffff:127.0.0.1]:9090",
    ],
)
def test_loopback_tricks_are_refused(private_off, url):
    with pytest.raises(ValueError, match="PROM_ALLOW_PRIVATE"):
        ops.assert_prom_url_safe(url)


def test_public_name_pointing_at_metadata_is_refused(monkeypatch):
    monkeypatch.setenv("PROM_ALLOW_PRIVATE", "1")  # metadata stays refused even then
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
    with patch("product.ops.socket.getaddrinfo", return_value=fake):
        with pytest.raises(ValueError, match="link-local"):
            ops.assert_prom_url_safe("http://169.254.169.254.nip.io")


def test_public_name_pointing_at_private_address_is_refused(private_off):
    fake = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0)),
    ]
    with patch("product.ops.socket.getaddrinfo", return_value=fake):
        with pytest.raises(ValueError, match="PROM_ALLOW_PRIVATE"):
            ops.assert_prom_url_safe("http://prom.example.com")


def test_unresolved_prometheus_host_is_refused(private_off):
    with patch("product.ops.socket.getaddrinfo", side_effect=OSError("nxdomain")):
        with pytest.raises(ValueError, match="did not resolve"):
            ops.assert_prom_url_safe("http://prom.example.com")


def test_fetch_dials_the_address_from_the_safety_check(monkeypatch):
    """The fetch must not resolve the name again after the check."""
    import socket

    from product.prometheus_adapter import fetch_memory_metrics

    monkeypatch.setenv("PROM_URL", "http://prom.example:9090")
    lookups = {"n": 0}
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    def lookup(*_a, **_k):
        lookups["n"] += 1
        return fake

    seen = {}

    def connect(address, timeout=None):
        seen["address"] = address
        raise OSError("stop after the dial")

    monkeypatch.setattr("product.ops.socket.getaddrinfo", lookup)
    monkeypatch.setattr("product.prometheus_adapter.socket.create_connection", connect)
    with pytest.raises(OSError, match="stop after the dial"):
        fetch_memory_metrics(hours=1)
    assert lookups["n"] == 1
    assert seen["address"] == ("93.184.216.34", 9090)


def test_public_name_with_public_addresses_is_allowed(private_off):
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    with patch("product.ops.socket.getaddrinfo", return_value=fake):
        ops.assert_prom_url_safe("http://prom.example.com:9090")


def test_prometheus_fetch_refuses_redirects():
    from product.prometheus_adapter import _RefuseRedirect

    handler = _RefuseRedirect()

    class _Req:
        full_url = "http://prom.example.com/api/v1/query_range"

    with pytest.raises(HTTPError, match="redirects are refused"):
        handler.redirect_request(_Req(), None, 302, "Found", {}, "http://169.254.169.254/")


def test_docker_defaults_refuse_private_prometheus():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "PROM_ALLOW_PRIVATE=0" in (root / "Dockerfile").read_text(encoding="utf-8")
    for env in ("test.env", "prod.env.example"):
        assert "PROM_ALLOW_PRIVATE=0" in (root / "deploy" / env).read_text(encoding="utf-8")
    assert "SHADOW_MODE=1" in (root / "deploy" / "prod.env.example").read_text(encoding="utf-8")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setenv("SHADOW_MODE", "1")
    for name in ("APP_PASSWORD", "ALLOW_OPEN_ACCESS", "INGEST_TOKEN", "ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    return app_mod.app


def test_repeated_bad_logins_lock_the_client(client, monkeypatch):
    from product.web.security import reset_login_attempts

    reset_login_attempts()
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    monkeypatch.setenv("LOGIN_MAX_FAILS", "3")
    monkeypatch.setenv("LOGIN_LOCK_SECONDS", "60")
    http = TestClient(client, client=("127.0.0.1", 5000), base_url="http://localhost")
    try:
        assert http.get("/connectors", auth=("oncall", "no")).status_code == 401
        assert http.get("/connectors", auth=("oncall", "no")).status_code == 401
        locked = http.get("/connectors", auth=("oncall", "no"))
        assert locked.status_code == 429
        assert locked.headers["retry-after"]
        # A correct password does not skip the lock.
        assert http.get("/connectors", auth=("oncall", "s3cret")).status_code == 429
        # Health checks stay open.
        assert http.get("/health").status_code == 200
    finally:
        reset_login_attempts()
    assert http.get("/connectors", auth=("oncall", "s3cret")).status_code == 200


def test_forwarded_request_needs_a_password(client, monkeypatch):
    """Behind a reverse proxy every client looks private."""
    c = TestClient(client, client=("127.0.0.1", 5000), base_url="http://localhost")
    assert c.get("/connectors").status_code == 200
    assert c.get("/connectors", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403
    assert c.get("/connectors", headers={"Forwarded": "for=8.8.8.8"}).status_code == 403
    assert c.get("/health", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 200
    monkeypatch.setenv("APP_PASSWORD", "pw")
    ok = c.get("/connectors", headers={"X-Forwarded-For": "8.8.8.8"}, auth=("oncall", "pw"))
    assert ok.status_code == 200


def test_loopback_client_needs_a_local_host_name(client, monkeypatch):
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends Host: evil.example."""
    c = TestClient(client, client=("127.0.0.1", 5000), base_url="http://evil.example")
    assert c.get("/connectors").status_code == 403
    monkeypatch.setenv("ALLOWED_HOSTS", "oncall.internal")
    ok = TestClient(client, client=("127.0.0.1", 5000), base_url="http://oncall.internal:8080")
    assert ok.get("/connectors").status_code == 200
    assert TestClient(client, client=("127.0.0.1", 5000), base_url="http://127.0.0.1:8080").get("/connectors").status_code == 200


def test_cross_site_posts_are_refused(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    c = TestClient(client, base_url="http://localhost:8080")
    body = {"shadow_mode": True}
    evil = c.post("/ops/shadow", json=body, auth=("oncall", "pw"), headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    null = c.post("/ops/shadow", json=body, auth=("oncall", "pw"), headers={"Origin": "null"})
    assert null.status_code == 403
    same = c.post("/ops/shadow", json=body, auth=("oncall", "pw"), headers={"Origin": "http://localhost:8080"})
    assert same.status_code == 200
    ref = c.post("/ops/shadow", json=body, auth=("oncall", "pw"), headers={"Referer": "https://evil.example/x"})
    assert ref.status_code == 403
    assert c.post("/ops/shadow", json=body, auth=("oncall", "pw")).status_code == 200  # curl: no Origin


def test_ingest_token_in_query_string_is_refused(client, monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "s3cret")
    c = TestClient(client)
    row = {"timestamp": "2026-02-01T10:00:00Z", "source": "web-01", "severity": "error", "message": "x"}
    assert c.post("/ingest/alerts?token=s3cret", json={"alerts": [row]}).status_code == 401
    ok = c.post("/ingest/alerts", json={"alerts": [row]}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_ops_changes_are_audited_without_secrets(client, monkeypatch, caplog):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    c = TestClient(client)
    with caplog.at_level(logging.INFO, logger="oncall.audit"):
        c.post("/ops/pagerduty/key", json={"routing_key": "rk_secret_value"}, auth=("oncall", "pw"))
        c.post("/ops/shadow", json={"shadow_mode": False, "confirm": "LIVE"}, auth=("mallory", "bad"))
    lines = [r.getMessage() for r in caplog.records if r.name == "oncall.audit"]
    assert any("POST /ops/pagerduty/key status=200 user=oncall" in line for line in lines)
    assert any("POST /ops/shadow status=401 user=mallory" in line for line in lines)
    assert not any("rk_secret_value" in line for line in lines)
