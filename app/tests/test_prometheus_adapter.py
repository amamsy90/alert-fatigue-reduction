"""Prometheus adapter maps query_range JSON into spike DataFrames."""
from __future__ import annotations

import json
import socket
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from product.prometheus_adapter import (
    MAX_POINTS,
    fetch_memory_metrics,
    probe_prometheus,
    prom_url_host,
    run_prometheus_test,
    service_from_labels,
    step_seconds,
)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Fake hostnames in these tests are not on the real DNS."""
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr("product.ops.socket.getaddrinfo", lambda *_a, **_k: fake)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _payload(result):
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def test_fetch_memory_metrics_parses_range(monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom.test")
    payload = _payload(
        [{"metric": {"service": "webservice1"}, "values": [[1609459200, "1000"], [1609459260, "2000"]]}]
    )
    with patch("product.prometheus_adapter.urlopen", return_value=_Resp(payload)):
        frame = fetch_memory_metrics(hours=1)
    assert len(frame) == 2
    assert list(frame["service"]) == ["webservice1", "webservice1"]
    assert frame["value"].iloc[1] == 2000.0


def test_default_range_stays_under_prometheus_point_limit(monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom.test")
    seen = {}

    def fake_urlopen(url, timeout):
        seen["url"] = url
        return _Resp(_payload([]))

    with patch("product.prometheus_adapter.urlopen", side_effect=fake_urlopen):
        fetch_memory_metrics()
    query = parse_qs(urlparse(seen["url"]).query)
    points = (float(query["end"][0]) - float(query["start"][0])) / float(query["step"][0])
    assert points <= MAX_POINTS
    assert step_seconds(24 * 14, 60) >= 121


def test_pod_hash_is_stripped_and_replicas_are_summed(monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom.test")
    assert service_from_labels({"pod": "webservice1-7d9f8c6b5-x2k4p"}) == "webservice1"
    payload = _payload(
        [
            {"metric": {"pod": "webservice1-7d9f8c6b5-x2k4p"}, "values": [[1609459200, "100"]]},
            {"metric": {"pod": "webservice1-7d9f8c6b5-q8m3z"}, "values": [[1609459200, "50"]]},
        ]
    )
    with patch("product.prometheus_adapter.urlopen", return_value=_Resp(payload)):
        frame = fetch_memory_metrics(hours=1)
    assert len(frame) == 1
    assert frame["service"].iloc[0] == "webservice1"
    assert frame["value"].iloc[0] == 150.0


def test_probe_reports_reachable_version(monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom.test")
    payload = {"status": "success", "data": {"version": "2.54.0"}}
    with patch("product.prometheus_adapter.urlopen", return_value=_Resp(payload)):
        out = probe_prometheus()
    assert out == {"reachable": True, "version": "2.54.0"}


def test_probe_does_not_echo_url_on_failure():
    out = probe_prometheus("http://user:secret@127.0.0.1:1")
    assert out["reachable"] is False
    assert "secret" not in str(out)


def test_prom_url_host_strips_credentials():
    assert prom_url_host("http://user:secret@prom.internal:9090") == "prom.internal"
    assert "secret" not in str(prom_url_host("http://user:secret@prom.internal:9090"))


def test_run_prometheus_test_ok_with_mocked_http(monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom.test:9090")
    build = {"status": "success", "data": {"version": "2.54.0"}}
    query = _payload(
        [
            {"metric": {"service": "web"}, "values": [[1609459200, "1"], [1609459260, "2"]]},
            {"metric": {"service": "api"}, "values": [[1609459200, "3"]]},
        ]
    )
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        if "buildinfo" in url:
            return _Resp(build)
        return _Resp(query)

    with patch("product.prometheus_adapter.urlopen", side_effect=fake_urlopen):
        out = run_prometheus_test()
    assert out["ok"] is True
    assert out["series_count"] == 2
    assert out["points"] == 3
    assert out["url_host"] == "prom.test"
    assert out["version"] == "2.54.0"
    assert "secret" not in str(out)
    assert any("query_range" in u for u in calls)


def test_run_prometheus_test_unreachable(monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom.test:9090")

    def boom(url, timeout=None):
        raise ConnectionError("nope")

    with patch("product.prometheus_adapter.urlopen", side_effect=boom):
        out = run_prometheus_test()
    assert out["ok"] is False
    assert out["series_count"] == 0
    assert out["points"] == 0
    assert out["url_host"] == "prom.test"
    assert out["error"] == "ConnectionError"


def test_run_prometheus_test_missing_url(monkeypatch):
    monkeypatch.delenv("PROM_URL", raising=False)
    out = run_prometheus_test()
    assert out["ok"] is False
    assert out["error"] == "PROM_URL is not set"
    assert out["url_host"] is None


def test_a_14_day_fetch_keeps_one_minute_points_so_live_jumps_are_confirmed(monkeypatch):
    """Requirement: a jump in live Prometheus data is detected. The jump rule links
    hot points at most `memory_gap_minutes` (2) apart, so the default 14-day fetch
    must return points that close together, within Prometheus' point limit per query."""
    import pandas as pd

    from src.config import Config
    from src.memory_spikes import detect_jumps

    monkeypatch.setenv("PROM_URL", "http://prom.test")
    now = pd.Timestamp.now(tz="UTC").timestamp()
    jump_from, jump_to = now - 6 * 3600, now - 6 * 3600 + 600  # 1 GB for 10 minutes, 6 hours ago
    queries = []

    def fake_prometheus(url, timeout):
        q = parse_qs(urlparse(url).query)
        start, end, step = float(q["start"][0]), float(q["end"][0]), float(q["step"][0])
        queries.append((start, end, step))
        assert (end - start) / step + 1 <= MAX_POINTS
        values, t = [], start
        while t <= end:
            values.append([t, str(2e9 + (1e9 if jump_from <= t < jump_to else 0))])
            t += step
        return _Resp(_payload([{"metric": {"service": "web"}, "values": values}]))

    with patch("product.prometheus_adapter.urlopen", side_effect=fake_prometheus):
        frame = fetch_memory_metrics()

    assert len(queries) > 1
    assert not frame.duplicated(["series_id", "timestamp"]).any()
    assert frame["timestamp"].diff().max() <= pd.Timedelta(minutes=Config().memory_gap_minutes)
    assert frame["value"].max() == 3e9  # no point summed twice where chunks meet
    jumps = detect_jumps(frame, Config())
    assert len(jumps) == 1
