"""Smoke test for a running on-call queue: the checks a deploy must pass before it is kept.

    python tools/smoke_test.py http://127.0.0.1:8080 --user oncall --password SECRET
    python tools/smoke_test.py http://127.0.0.1:8080 --user oncall --password SECRET --no-write

Uses only the standard library, so it runs anywhere a deploy runs. Exit code 0 when every
check passes, 1 otherwise. Each check prints one line.

Checks (the ones marked [write] run a sample job, which adds a job to the database; use
--no-write against a system whose queue you do not want to touch):

  1. /health answers 200.
  2. Shadow mode is on (unless --allow-live): a fresh deploy must not be able to page.
  3. The version reported matches --expect-version, when given.
  4. The password is enforced: a request without credentials is refused.
  5. /monitoring answers with credentials, and lists the drift and challenger checks.
  6. /metrics serves Prometheus text with the health gauge.
  7. [write] A sample job runs and the queue is not empty.
  8. [write] POST /models/retrain answers 200 (the auth path a scheduler uses).
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

# (method, path, json body, send credentials) -> (HTTP status, body text)
Transport = Callable[[str, str, dict | None, bool], tuple[int, str]]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def run_smoke(
    call: Transport,
    *,
    expect_version: str | None = None,
    write: bool = True,
    check_auth: bool = True,
    allow_live: bool = False,
) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> bool:
        checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    def get_json(path: str, authed: bool = True) -> tuple[int, dict]:
        status, body = call("GET", path, None, authed)
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}

    status, health = get_json("/health", authed=False)
    if not add("health answers 200", status == 200, f"status {status}"):
        return checks  # nothing else can be judged
    add("shadow mode is on", allow_live or health.get("shadow_mode") is True,
        f"shadow_mode={health.get('shadow_mode')}")
    if expect_version:
        got = health.get("version")
        add("version matches", got == expect_version, f"expected {expect_version}, got {got}")

    if check_auth:
        status, _ = call("GET", "/monitoring", None, False)
        add("password is enforced", status in (401, 403), f"unauthenticated /monitoring gave {status}")

    status, monitoring = get_json("/monitoring")
    checks_present = set(monitoring.get("checks", {}))
    add("monitoring answers", status == 200 and monitoring.get("status") in ("ok", "warn"),
        f"status {status}, verdict {monitoring.get('status')}")
    add("drift and challenger checks are present", {"input_drift", "challenger_shadow"} <= checks_present,
        f"checks: {sorted(checks_present)}")

    status, text = call("GET", "/metrics", None, True)
    add("metrics are served", status == 200 and "oncall_health_ok" in text, f"status {status}")

    if write:
        status, job = call("POST", "/jobs/run", {"mode": "sample", "budget_k": 20}, True)
        add("a sample job runs", status == 200, f"status {status}")
        status, queue = get_json("/queue")
        add("the queue is not empty", status == 200 and queue.get("count", 0) + len(queue.get("jumps", [])) > 0,
            f"status {status}, {queue.get('count')} items")
        status, _ = call("POST", "/models/retrain", {}, True)
        add("retrain route answers", status == 200, f"status {status}")
    return checks


def http_transport(base_url: str, user: str | None, password: str | None, timeout: float = 60.0) -> Transport:
    token = None
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(method: str, path: str, body: dict | None, authed: bool) -> tuple[int, str]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if authed and token:
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            return 0, str(exc)

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--expect-version")
    parser.add_argument("--no-write", action="store_true", help="skip the sample job and retrain checks")
    parser.add_argument("--no-auth-check", action="store_true", help="skip the unauthenticated-request check")
    parser.add_argument("--allow-live", action="store_true", help="do not require shadow mode")
    args = parser.parse_args(argv)
    call = http_transport(args.base_url, args.user, args.password)
    checks = run_smoke(call, expect_version=args.expect_version, write=not args.no_write,
                       check_auth=not args.no_auth_check, allow_live=args.allow_live)
    for c in checks:
        print(f"[{'ok' if c.ok else 'FAIL'}] {c.name}" + (f"  ({c.detail})" if c.detail and not c.ok else ""))
    failed = [c for c in checks if not c.ok]
    print("PASS" if not failed else f"FAIL: {len(failed)} of {len(checks)} checks")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
