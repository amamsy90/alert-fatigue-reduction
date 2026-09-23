"""Feature extraction uses only the past."""
import pandas as pd

from src.entities import Alert
from src.features import build_feature_frame, normalize_message

T0 = pd.Timestamp("2026-01-05 10:00:00", tz="UTC")


def _alert(aid, minutes, source="db-01", message="cpu spike on host"):
    return Alert(
        alert_id=aid,
        timestamp=T0 + pd.Timedelta(minutes=minutes),
        source=source,
        alert_type="cpu",
        severity="warning",
        message=message,
    )


def test_normalize_strips_ids_and_numbers():
    text = normalize_message(
        "2021-07-01 11:54:27,616 | WARNING | 0.0.0.4 | abcdef0123456789 | wait 11 seconds"
    )
    assert "2021" not in text
    assert "0.0.0.4" not in text
    assert "abcdef0123456789" not in text
    assert "wait" in text and "seconds" in text


def test_counts_use_only_earlier_alerts():
    alerts = [_alert("a1", 0), _alert("a2", 1), _alert("a3", 2), _alert("a4", 30)]
    frame = build_feature_frame(alerts).set_index("alert_id")
    assert frame.loc["a1", "count_5m"] == 0
    assert frame.loc["a2", "count_5m"] == 1
    assert frame.loc["a3", "count_5m"] == 2
    assert frame.loc["a4", "count_5m"] == 0
    assert frame.loc["a4", "count_60m"] == 3
    assert frame.loc["a2", "since_last_sec"] == 60


def test_other_sources_do_not_inflate_counts():
    alerts = [_alert("a1", 0, source="db-01"), _alert("a2", 1, source="web-01")]
    frame = build_feature_frame(alerts).set_index("alert_id")
    assert frame.loc["a2", "count_5m"] == 0
