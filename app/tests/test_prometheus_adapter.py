"""Prometheus adapter maps query_range JSON into spike DataFrames."""
from __future__ import annotations

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from product.prometheus_adapter import (
    MAX_POINTS,
    fetch_memory_metrics,
    service_from_labels,
    step_seconds,
)


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
