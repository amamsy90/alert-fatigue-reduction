"""tools/smoke_test.py: the deploy gate passes a correct deployment and fails each unsafe one."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SPEC = importlib.util.spec_from_file_location("smoke_test", Path(__file__).parents[2] / "tools" / "smoke_test.py")
smoke = importlib.util.module_from_spec(SPEC)
sys.modules["smoke_test"] = smoke  # a dataclass looks its module up here
SPEC.loader.exec_module(smoke)

USER, PASSWORD = "oncall", "s3cret-for-tests"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH", "AUTH_DB"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("APP_USER", USER)
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SHADOW_MODE", "1")
    monkeypatch.delenv("ALLOW_OPEN_ACCESS", raising=False)
    import product.app as app_mod
    from product.store import QueueStore

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    return TestClient(app_mod.app)


def _transport(client, *, tamper=None):
    def call(method, path, body, authed):
        response = client.request(method, path, json=body, auth=(USER, PASSWORD) if authed else None)
        text = response.text
        if tamper and path == "/health":
            text = tamper(text)
        return response.status_code, text

    return call


def _failed(checks):
    return [c.name for c in checks if not c.ok]


def test_a_correct_deployment_passes_every_check(client):
    from product import __version__

    checks = smoke.run_smoke(_transport(client), expect_version=__version__)
    assert _failed(checks) == [], [(c.name, c.detail) for c in checks if not c.ok]
    assert len(checks) >= 9


def test_no_write_mode_does_not_run_a_job(client):
    checks = smoke.run_smoke(_transport(client), write=False)
    assert _failed(checks) == []
    assert not any("sample job" in c.name for c in checks)
    assert client.get("/queue", auth=(USER, PASSWORD)).status_code == 404  # still no job in the database


def test_live_mode_fails_the_gate_unless_allowed(client):
    def live(text):
        return json.dumps({**json.loads(text), "shadow_mode": False})

    assert "shadow mode is on" in _failed(smoke.run_smoke(_transport(client, tamper=live), write=False))
    assert "shadow mode is on" not in _failed(
        smoke.run_smoke(_transport(client, tamper=live), write=False, allow_live=True))


def test_a_wrong_version_fails(client):
    assert "version matches" in _failed(smoke.run_smoke(_transport(client), expect_version="0.0.1", write=False))


def test_an_unprotected_deployment_fails(client, monkeypatch):
    """Without a password a private client is let in, so the unauthenticated request succeeds."""
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("ALLOW_OPEN_ACCESS", "1")
    assert "password is enforced" in _failed(smoke.run_smoke(_transport(client), write=False))


def test_an_unreachable_service_stops_at_the_first_check():
    def down(method, path, body, authed):
        return 0, "connection refused"

    checks = smoke.run_smoke(down)
    assert [c.name for c in checks] == ["health answers 200"] and not checks[0].ok


def test_the_command_line_exit_code_follows_the_result(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "http_transport", lambda *a, **k: (lambda m, p, b, a_: (0, "down")))
    assert smoke.main(["http://nowhere.invalid"]) == 1
    assert "FAIL" in capsys.readouterr().out
