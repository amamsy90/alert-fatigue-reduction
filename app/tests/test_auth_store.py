"""Sessions and the login lock survive a restart (product/web/auth_store.py).

Each "process" below is a separate Python interpreter importing the app, so
nothing is shared in memory: only auth.db carries state from one to the next.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]

_SCRIPT = """
import json, sys
from fastapi.testclient import TestClient
import product.app as app_mod
from product.store import QueueStore
app_mod.use_store(QueueStore(sys.argv[2]))
http = TestClient(app_mod.app, client=("127.0.0.1", 5000), base_url="http://localhost")
step = json.loads(sys.argv[1])
out = {}
if step["do"] == "login":
    for _ in range(step.get("times", 1)):
        r = http.post("/login", json={"username": "oncall", "password": step["password"]})
    out = {"status": r.status_code, "cookie": r.cookies.get("oncall_session")}
elif step["do"] == "get":
    if step.get("cookie"):
        http.cookies.set("oncall_session", step["cookie"])
    out = {"status": http.get("/connectors").status_code}
print(json.dumps(out))
"""


def _process(tmp_path: Path, step: dict, **env: str) -> dict:
    """Run one fresh interpreter (one app start) and return its JSON result."""
    full_env = {
        **os.environ,
        "AUTH_DB": str(tmp_path / "auth.db"),
        "OPS_PATH": str(tmp_path / "ops.json"),
        "QUEUE_DB": str(tmp_path / "queue.db"),
        "APP_PASSWORD": "pw",
        **env,
    }
    done = subprocess.run(
        [sys.executable, "-c", _SCRIPT, json.dumps(step), str(tmp_path / "queue.db")],
        cwd=APP_DIR, env=full_env, capture_output=True, text=True, timeout=120, check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_signed_in_user_stays_signed_in_after_a_restart(tmp_path):
    """Requirement: a restart of the app does not sign the on-call engineer out."""
    signed = _process(tmp_path, {"do": "login", "password": "pw"})
    assert signed["status"] == 200 and signed["cookie"]
    after_restart = _process(tmp_path, {"do": "get", "cookie": signed["cookie"]})
    assert after_restart["status"] == 200


def test_a_login_lock_survives_a_restart(tmp_path):
    """Requirement: restarting the app does not clear a lock earned by guessing passwords."""
    locked = _process(tmp_path, {"do": "login", "password": "wrong", "times": 3}, LOGIN_MAX_FAILS="3")
    assert locked["status"] == 429
    assert _process(tmp_path, {"do": "login", "password": "pw"}, LOGIN_MAX_FAILS="3")["status"] == 429


def test_the_session_file_holds_no_usable_cookie(tmp_path, monkeypatch):
    """Requirement: a copy of auth.db cannot be used to sign in."""
    from product.web import auth_store

    monkeypatch.setenv("AUTH_DB", str(tmp_path / "auth.db"))
    token = "a-cookie-value-that-must-not-be-stored"
    auth_store.add_session(token, "oncall", "operator", expires=100.0, now=0.0, max_sessions=10)
    assert token.encode() not in (tmp_path / "auth.db").read_bytes()


def test_a_session_ends_when_it_expires(tmp_path, monkeypatch):
    from product.web import auth_store

    monkeypatch.setenv("AUTH_DB", str(tmp_path / "auth.db"))
    auth_store.add_session("tok", "oncall", "operator", expires=100.0, now=0.0, max_sessions=10)
    assert auth_store.get_session("tok", now=99.0) == ("oncall", "operator")
    assert auth_store.get_session("tok", now=100.0) is None
    assert auth_store.get_session("tok", now=50.0) is None  # deleted once it expired


def test_the_oldest_sessions_go_past_the_cap(tmp_path, monkeypatch):
    from product.web import auth_store

    monkeypatch.setenv("AUTH_DB", str(tmp_path / "auth.db"))
    for i in range(4):
        auth_store.add_session(f"t{i}", "oncall", "operator", expires=100.0 + i, now=0.0, max_sessions=3)
    assert [auth_store.get_session(f"t{i}", now=1.0) is not None for i in range(4)] == [False, True, True, True]
