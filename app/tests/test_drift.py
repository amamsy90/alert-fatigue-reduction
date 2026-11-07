"""Drift checks: expected values come from the PSI definition and from constructed windows."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.drift import (
    MIN_ALERTS,
    category_psi,
    drift_report,
    hourly_counts,
    numeric_psi,
    text_drift,
)
from src.entities import Alert

T0 = pd.Timestamp("2026-03-02 00:00", tz="UTC")


def _alerts(n, *, start=0, every_minutes=6, severity="info", source="web", kind="log",
            messages=("cache refresh done", "request served ok"), fault=None):
    rows = []
    for i in range(n):
        rows.append(Alert(f"a{start + i}", T0 + pd.Timedelta(minutes=every_minutes * (start + i)),
                          source, kind, severity, messages[i % len(messages)], fault))
    return rows


def test_psi_of_a_window_with_itself_is_zero():
    assert category_psi(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == pytest.approx(0.0)
    assert numeric_psi(list(range(100)), list(range(100))) == pytest.approx(0.0, abs=1e-9)


def test_category_psi_matches_the_formula_by_hand():
    # PSI = sum (cur - ref) * ln(cur / ref); 0.5/0.5 -> 0.9/0.1:
    # 0.4 * ln(1.8) + (-0.4) * ln(0.2) = 0.2351 + 0.6438 = 0.8789
    reference = ["a"] * 50 + ["b"] * 50
    current = ["a"] * 90 + ["b"] * 10
    assert category_psi(reference, current) == pytest.approx(0.4 * math.log(1.8) - 0.4 * math.log(0.2), rel=1e-6)


def test_category_psi_counts_a_new_category_as_a_large_shift():
    assert category_psi(["info"] * 100, ["info"] * 50 + ["critical"] * 50) > 0.25


def test_numeric_psi_separates_a_shifted_window_from_a_resample():
    rng = np.random.default_rng(1)
    reference = rng.normal(100, 10, 2000)
    same = rng.normal(100, 10, 2000)
    shifted = rng.normal(125, 10, 2000)
    assert numeric_psi(reference, same) < 0.1
    assert numeric_psi(reference, shifted) > 0.25


def test_hourly_counts_include_empty_hours():
    alerts = [Alert("1", T0, "s", "log", "info", "m"), Alert("2", T0 + pd.Timedelta(hours=3), "s", "log", "info", "m")]
    assert list(hourly_counts(alerts)) == [1, 0, 0, 1]


def test_text_test_is_blind_to_the_same_wording_and_sure_of_new_wording():
    same = ["cache refresh done", "request served ok", "heartbeat received"] * 30
    assert text_drift(same, same[::-1], seed=0)["auc"] < 0.65
    new = ["out of memory killer invoked", "disk quota exceeded on volume", "connection pool exhausted"] * 30
    result = text_drift(same, new, seed=0)
    assert result["auc"] > 0.95
    assert result["top_terms"], "the terms that moved should be named"


def test_text_test_does_not_judge_a_tiny_window():
    assert text_drift(["a b"] * 5, ["c d"] * 5) is None


def test_a_small_window_is_not_judged():
    report = drift_report(_alerts(MIN_ALERTS - 1), _alerts(MIN_ALERTS))
    assert report["status"] == "n/a"


def test_a_stable_stream_is_ok():
    report = drift_report(_alerts(200), _alerts(200, start=200))
    assert report["status"] == "ok", report["reasons"]
    assert report["psi_max"] < 0.1


def test_a_severity_flip_is_a_warning_and_says_which_feature():
    report = drift_report(_alerts(200), _alerts(200, start=200, severity="critical"))
    assert report["status"] == "warn"
    assert any("severity" in r for r in report["reasons"])


def test_a_burst_changes_the_arrival_rate():
    quiet = _alerts(240, every_minutes=6)           # 10 an hour
    burst = _alerts(480, start=240, every_minutes=1)  # 60 an hour
    report = drift_report(quiet, burst)
    assert report["status"] == "warn"
    assert any("arrival rate" in r for r in report["reasons"])


def test_new_wording_is_a_warning_with_the_new_terms():
    new = ("out of memory killer invoked", "disk quota exceeded on volume")
    report = drift_report(_alerts(200), _alerts(200, start=200, messages=new))
    assert report["status"] == "warn"
    assert report["text_auc"] > 0.95
    assert report["text_top_terms"]


def test_labels_never_change_the_report():
    """A drift check reports; it must not depend on which alerts turned out to be faults."""
    plain = drift_report(_alerts(100), _alerts(100, start=100))
    labelled = drift_report(_alerts(100, fault="f1"), _alerts(100, start=100, fault="f2"))
    assert plain == labelled


def test_the_report_is_repeatable_and_json_safe():
    import json

    a, b = _alerts(100), _alerts(100, start=100, severity="warning")
    first = drift_report(a, b, seed=3)
    assert first == drift_report(a, b, seed=3)
    json.dumps(first)


# --- an ingest job is compared with what earlier jobs stored ---------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    for name in ("APP_PASSWORD", "ALLOW_OPEN_ACCESS"):
        monkeypatch.delenv(name, raising=False)
    import product.app as app_mod
    from product.store import QueueStore

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    return TestClient(app_mod.app)


def _day(day: int, *, severity="info", message="cache refresh done on worker", n=70):
    base = pd.Timestamp("2026-03-02", tz="UTC") + pd.Timedelta(days=day)
    return [{"timestamp": (base + pd.Timedelta(minutes=20 * i)).isoformat(), "source": f"svc{i % 3}",
             "severity": severity, "message": message} for i in range(n)]


def _ingest(client, rows):
    client.post("/ingest/alerts", json={"alerts": rows})
    return client.post("/jobs/run", json={"mode": "ingest", "budget_k": 20}).json()["meta"]["drift"]


def test_a_day_is_judged_against_earlier_days_not_against_its_own_halves(client):
    first = _ingest(client, _day(0))
    assert first["reference"] == "early versus later part of this batch"  # no history yet: the only comparison there is
    _ingest(client, _day(1))
    third = _ingest(client, _day(2))
    assert third["reference"] == "alerts stored by earlier jobs"
    assert third["status"] == "ok", third["reasons"]  # an ordinary day after ordinary days


def test_a_changed_day_warns_against_the_history(client):
    for day in range(3):
        _ingest(client, _day(day))
    changed = _ingest(client, _day(3, severity="critical", message="out of memory killer invoked on worker"))
    assert changed["status"] == "warn"
    assert any("severity" in r for r in changed["reasons"]) and changed["text_auc"] > 0.9
