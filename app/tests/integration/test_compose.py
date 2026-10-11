"""The app beside a real Prometheus and Alertmanager (integration/docker-compose.yml).

Skipped unless INTEGRATION_BASE_URL is set, so `pytest` alone never needs Docker.
Requirement: an alert fired in Alertmanager reaches the app through its webhook,
the ingest job scores it with memory series pulled from Prometheus, and nothing
is paged in shadow mode.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

BASE = os.environ.get("INTEGRATION_BASE_URL", "").rstrip("/")
PROM = os.environ.get("INTEGRATION_PROMETHEUS_URL", "http://127.0.0.1:19090").rstrip("/")
ALERTMANAGER = os.environ.get("INTEGRATION_ALERTMANAGER_URL", "http://127.0.0.1:19093").rstrip("/")
AUTH = "Basic " + base64.b64encode(b"oncall:integration-password").decode()

pytestmark = pytest.mark.skipif(not BASE, reason="set INTEGRATION_BASE_URL to run against the compose stack")


def _get(url: str, auth: bool = False) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": AUTH} if auth else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _post(url: str, body: object) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def _wait(what: str, check, timeout: float = 300.0, every: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(every)


def _prometheus_has_points() -> bool:
    """True once Prometheus holds about 90 s of samples (5 s scrapes), so the app's
    60 s grid has at least one point in it."""
    params = urllib.parse.urlencode({"query": "count_over_time(process_resident_memory_bytes[5m])"})
    status, body = _get(f"{PROM}/api/v1/query?{params}")
    result = body.get("data", {}).get("result", []) if status == 200 else []
    return bool(result) and float(result[0]["value"][1]) >= 18


def test_alertmanager_alert_is_scored_with_prometheus_metrics_and_not_paged():
    _wait("app /health", lambda: _get(f"{BASE}/health")[0] == 200, timeout=120)
    _wait("Prometheus to hold a scraped point", _prometheus_has_points)

    status, probe = _get(f"{BASE}/ops/prometheus/test", auth=True)
    assert status == 200 and probe.get("ok"), probe

    now = datetime.now(timezone.utc)
    alert = {
        "labels": {"alertname": "IntegrationMemoryHigh", "service": "prometheus", "severity": "critical"},
        "annotations": {"summary": "integration test alert"},
        "startsAt": now.isoformat(),
        "endsAt": (now + timedelta(minutes=10)).isoformat(),
    }
    assert _post(f"{ALERTMANAGER}/api/v2/alerts", [alert]) == 200

    def scored_job():
        status, queue = _get(f"{BASE}/queue", auth=True)
        if status != 200:
            return None
        job = queue["job"]
        return queue if job["source"] == "ingest" and job["meta"].get("alert_count", 0) >= 1 else None

    queue = _wait("the ingest job from Alertmanager's webhook", scored_job, timeout=120)
    meta = queue["job"]["meta"]
    assert meta["has_metrics"] is True, "the ingest job did not pull Prometheus series"
    assert meta["metric_medians"], "no per-series median from Prometheus"
    # The row carries the alert's summary as its message and its service label as its source.
    rows = [i for i in queue["items"] if i["message"] == "integration test alert"]
    assert rows and rows[0]["source"] == "prometheus" and rows[0]["severity"] == "critical", queue["items"]
    assert queue["job"]["shadow_mode"] is True
    assert meta["pagerduty"] == {"sent": 0, "failed": 0, "skipped": "shadow mode"}
    assert meta["would_have_paged"]["alerts"] >= 1, "shadow mode should record what Live would have paged"
