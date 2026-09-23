"""Alertmanager webhooks become the same rows the queue already scores."""
from product.alertmanager import from_alertmanager, looks_like_alertmanager, normalize_ingest

AM = {
    "version": "4",
    "status": "firing",
    "receiver": "oncall-queue",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "HighMemory",
                "severity": "warning",
                "service": "webservice1",
            },
            "annotations": {"description": "container working set jumped"},
            "startsAt": "2026-01-12T10:00:00.000Z",
            "fingerprint": "abc123",
        },
        {
            "status": "resolved",
            "labels": {"alertname": "HighMemory", "service": "webservice1"},
            "annotations": {"summary": "cleared"},
            "startsAt": "2026-01-12T09:00:00.000Z",
            "fingerprint": "old",
        },
    ],
}


def test_alertmanager_keeps_firing_alerts_and_skips_resolved():
    rows = from_alertmanager(AM)
    assert len(rows) == 1
    assert rows[0]["source"] == "webservice1"
    assert rows[0]["alert_type"] == "HighMemory"
    assert rows[0]["severity"] == "warning"
    assert rows[0]["message"] == "container working set jumped"
    assert rows[0]["alert_id"] == "abc123"
    assert rows[0]["timestamp"].startswith("2026-01-12T10:00")


def test_normalize_accepts_both_shapes():
    assert looks_like_alertmanager(AM)
    ours = {"alerts": [{"timestamp": "2026-01-12T10:00:00Z", "source": "db", "severity": "info"}]}
    assert not looks_like_alertmanager(ours)
    assert len(normalize_ingest(ours)) == 1
    assert normalize_ingest(AM)[0]["source"] == "webservice1"
