from __future__ import annotations

from homelab.collect import (
    alerts_from_prometheus,
    firing_episodes,
    logs_from_loki,
    metrics_from_prometheus,
    spans_from_otlp,
    trace_ids,
    write_table,
)
from homelab.companies import load_companies


def test_firing_episodes_start_a_row_per_stretch():
    episodes = firing_episodes([[10, "0"], [70, "1"], [130, "1"], [190, "0"], [250, "1"]])
    assert episodes == [(70.0, 190.0), (250.0, 250.0)]


def test_alerts_metrics_logs_and_spans_carry_the_company():
    companies = load_companies()
    alerts = alerts_from_prometheus(
        {
            "data": {
                "result": [
                    {
                        "metric": {
                            "alertname": "HomelabCpuHigh",
                            "namespace": "company-acme",
                            "service": "frontend-abcde-fghij",
                            "severity": "warning",
                        },
                        "values": [[1_700_000_000, "1"], [1_700_000_060, "0"]],
                    },
                    {
                        "metric": {"alertname": "Watchdog", "severity": "none"},
                        "values": [[1_700_000_000, "1"]],
                    },
                ]
            }
        },
        companies,
    )
    assert alerts[0]["company"] == "acme"
    assert alerts[0]["source"] == "acme/frontend"
    assert alerts[1]["company"] == "cluster"
    assert all(row["company"] for row in alerts)

    metrics = metrics_from_prometheus(
        {
            "data": {
                "result": [
                    {
                        "metric": {"namespace": "company-borealis", "pod": "borealis-frontend-abcde-fghij"},
                        "values": [[1_700_000_000, "100"], [1_700_000_000, "50"]],
                    },
                    {
                        "metric": {"namespace": "company-borealis", "pod": "borealis-frontend-zzzzz-yyyyy"},
                        "values": [[1_700_000_000, "25"]],
                    },
                ]
            }
        },
        companies,
    )
    assert len(metrics) == 1
    assert metrics[0]["company"] == "borealis"
    assert metrics[0]["service"] == "borealis/frontend"
    assert metrics[0]["value"] == 175

    logs = logs_from_loki(
        {
            "data": {
                "result": [
                    {
                        "stream": {"namespace": "company-cassini", "pod": "front-end-abcde-fghij"},
                        "values": [["1700000000000000000", "error: checkout failed"]],
                    }
                ]
            }
        },
        companies,
    )
    assert logs[0]["company"] == "cassini"
    assert logs[0]["source"] == "cassini/front-end"
    assert logs[0]["severity"] == "error"

    spans = spans_from_otlp(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "frontend"}},
                            {"key": "company", "value": {"stringValue": "acme"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "abc",
                                    "spanId": "def",
                                    "startTimeUnixNano": 1_700_000_000_000_000_000,
                                    "endTimeUnixNano": 1_700_000_000_050_000_000,
                                    "status": {"code": 2},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    )
    assert spans[0]["company"] == "acme"
    assert spans[0]["serviceName"] == "acme/frontend"
    assert spans[0]["statusCode"] == "ERROR"
    assert spans[0]["duration"] == 50


def test_write_table_counts_new_rows(tmp_path):
    from homelab.collect import ALERT_COLUMNS

    path = tmp_path / "alerts.csv"
    first = write_table(
        path,
        [{"alert_id": "a", "timestamp": "t", "source": "acme/frontend", "alert_type": "x", "severity": "warning", "message": "m", "company": "acme"}],
        ALERT_COLUMNS,
        ["alert_id"],
    )
    second = write_table(
        path,
        [{"alert_id": "a", "timestamp": "t", "source": "acme/frontend", "alert_type": "x", "severity": "warning", "message": "m", "company": "acme"}],
        ALERT_COLUMNS,
        ["alert_id"],
    )
    assert first == 1
    assert second == 0


def test_tempo_search_ids():
    assert trace_ids({"traces": [{"traceID": "abc"}, {"traceId": "def"}, {}]}) == ["abc", "def"]


def _alerts_payload(values):
    return {
        "data": {
            "result": [
                {
                    "metric": {"alertname": "HomelabMemoryHigh", "namespace": "company-acme", "service": "adservice", "severity": "warning"},
                    "values": values,
                }
            ]
        }
    }


def test_firing_episodes_split_on_missing_samples():
    # ALERTS has no 0 samples: a resolved alert just stops reporting.
    episodes = firing_episodes([[0, "1"], [60, "1"], [600, "1"], [660, "1"]])
    assert episodes == [(0.0, 60.0), (600.0, 660.0)]


def test_still_firing_alert_is_one_row_across_overlapping_passes(tmp_path):
    companies = load_companies()
    state: dict = {}
    out = tmp_path / "alerts.csv"
    # Pass 1 window 0..600, pass 2 re-reads from 300 (the 5-minute overlap) to 900.
    first = alerts_from_prometheus(_alerts_payload([[t, "1"] for t in range(0, 601, 60)]), companies, state)
    second = alerts_from_prometheus(_alerts_payload([[t, "1"] for t in range(300, 901, 60)]), companies, state)
    assert write_table(out, first, ("alert_id", "timestamp", "source", "alert_type", "severity", "message", "company"), ["alert_id"]) == 1
    assert write_table(out, second, ("alert_id", "timestamp", "source", "alert_type", "severity", "message", "company"), ["alert_id"]) == 0
    assert first[0]["alert_id"] == second[0]["alert_id"]


def test_refire_after_gap_is_a_new_row():
    companies = load_companies()
    state: dict = {}
    first = alerts_from_prometheus(_alerts_payload([[0, "1"], [60, "1"]]), companies, state)
    # Overlap re-reads the old stretch, then the alert fires again 10 minutes later.
    second = alerts_from_prometheus(_alerts_payload([[60, "1"], [660, "1"], [720, "1"]]), companies, state)
    starts = [row["timestamp"] for row in second]
    assert starts[0] == first[0]["timestamp"]
    assert len({row["alert_id"] for row in first + second}) == 2
