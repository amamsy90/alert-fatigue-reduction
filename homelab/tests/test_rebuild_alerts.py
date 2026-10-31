"""Rebuild alert episodes from chunked Prometheus matrices. No network."""
from __future__ import annotations

from homelab.companies import load_companies
from homelab.rebuild_alerts import episodes_from_matrix, merge_chunks

METRIC = {
    "alertname": "HomelabMemoryHigh",
    "namespace": "company-acme",
    "service": "adservice",
    "severity": "warning",
}


def _body(values):
    return {"data": {"result": [{"metric": METRIC, "values": values}]}}


def test_chunks_rebuild_one_episode_across_the_boundary_and_two_after_a_gap():
    companies = load_companies()
    # 60 s samples split across two query_range chunks stay one firing stretch.
    continuous = merge_chunks([
        _body([[0, "1"], [60, "1"]]),
        _body([[120, "1"], [180, "1"]]),
    ])
    one = episodes_from_matrix(continuous, companies)
    assert list(one["timestamp"]) == ["1970-01-01T00:00:00Z"]
    assert list(one["source"]) == ["acme/adservice"]
    assert len(one) == 1

    # A hole longer than the 120 s gap rule is a second episode, even inside one chunk.
    gapped = merge_chunks([_body([[0, "1"], [60, "1"], [600, "1"], [660, "1"]])])
    two = episodes_from_matrix(gapped, companies)
    assert list(two["timestamp"]) == ["1970-01-01T00:00:00Z", "1970-01-01T00:10:00Z"]
    assert two["alert_id"].nunique() == 2
