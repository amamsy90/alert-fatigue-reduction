"""Alert features (design spec section 4.2).

Every feature uses only the alert itself and alerts that arrived earlier, so
scoring the test week cannot see the future. `during_memory_spike` is 1 only
once the jump is confirmed (a live system cannot know it earlier).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict, deque

import pandas as pd

from .config import Config
from .entities import Alert

_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d+")
_IP_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")
_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+")

NUMERIC_COLUMNS = [
    "severity_rank",
    "count_5m",
    "count_15m",
    "count_60m",
    "since_last_sec",
    "hour_sin",
    "hour_cos",
    "msg_len",
    "has_fail",
    "has_memory",
    "has_error",
    "during_memory_spike",
]


def normalize_message(message: str) -> str:
    """Strip timestamps, addresses, and ids so similar alerts share a template."""
    text = message.lower()
    text = _TS_RE.sub(" ", text)
    text = _IP_RE.sub(" ", text)
    text = _HEX_RE.sub(" ", text)
    text = _NUM_RE.sub(" ", text)
    return " ".join(text.split())


def build_feature_frame(
    alerts: list[Alert],
    config: Config | None = None,
    spikes: list | None = None,
) -> pd.DataFrame:
    """One row per alert, in the same order. Alerts must be time-sorted.

    `spikes` are memory episodes. An alert is marked when it falls inside a
    spike on the same service, after the spike was confirmed.
    """
    config = config or Config()
    spikes_by_service: dict[str, list] = defaultdict(list)
    for spike in spikes or []:
        spikes_by_service[spike.service].append(spike)
    windows = (5, 15, 60)
    history: dict[int, dict[str, deque]] = {
        minutes: defaultdict(deque) for minutes in windows
    }
    last_seen: dict[str, pd.Timestamp] = {}
    rows: list[dict] = []

    for alert in alerts:
        counts = {}
        for minutes in windows:
            recent = history[minutes][alert.source]
            cutoff = alert.timestamp - pd.Timedelta(minutes=minutes)
            while recent and recent[0] < cutoff:
                recent.popleft()
            counts[minutes] = len(recent)
            recent.append(alert.timestamp)

        previous = last_seen.get(alert.source)
        since_last = (
            (alert.timestamp - previous).total_seconds() if previous is not None else -1.0
        )
        last_seen[alert.source] = alert.timestamp

        text = normalize_message(alert.message)
        hour = alert.timestamp.hour + alert.timestamp.minute / 60.0
        rows.append(
            {
                "alert_id": alert.alert_id,
                "source": alert.source,
                "severity": alert.severity,
                "message_norm": text,
                "label": 1 if alert.fault_id else 0,
                "severity_rank": config.severity_rank.get(alert.severity, 0),
                "count_5m": counts[5],
                "count_15m": counts[15],
                "count_60m": counts[60],
                "since_last_sec": since_last,
                "hour_sin": math.sin(2 * math.pi * hour / 24.0),
                "hour_cos": math.cos(2 * math.pi * hour / 24.0),
                "msg_len": len(text),
                "has_fail": int("fail" in text or "error" in text),
                "has_memory": int("memory" in text),
                "has_error": int(alert.severity in ("error", "critical")),
                "during_memory_spike": int(
                    any(
                        spike.open_at(alert.timestamp)
                        for spike in spikes_by_service.get(alert.source, [])
                        if getattr(spike, "kind", "jump") != "gap"
                    )
                ),
            }
        )
    return pd.DataFrame(rows)
