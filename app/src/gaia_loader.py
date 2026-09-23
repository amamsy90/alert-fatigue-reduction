"""Load GAIA MicroSS "run" logs (run_table_*.csv) into alerts + faults.

Each row is one system log line. The injected-fault ground truth is embedded
in the message text, e.g.:

    [memory_anomalies] trigger a high memory program, start at
    2021-07-01 11:44:26.882752 and lasts 600 seconds and use 1g memory

A row is a FAULT when its message matches an injection pattern (bracket tag +
explicit start time and duration). All other rows are ALERTS (noise). Each
alert is labelled with the fault whose time window covers it on the same
service; unmatched alerts are noise.

Clocks in these files are China Standard Time (UTC+8) with no timezone
written down. Memory metrics are Unix time, so every log timestamp is
converted to UTC before anything is aligned.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .entities import Alert, Fault

INJECTION_RE = re.compile(
    r"\[(?P<tag>[^\]]+)\].*?start at (?P<start>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r" and lasts (?P<duration>\d+) seconds",
    re.DOTALL,
)
LOG_HEADER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| (?P<level>[A-Z]+) \|"
)
LOG_TZ = "Asia/Shanghai"


def _log_time_to_utc(text: str) -> pd.Timestamp:
    """Interpret a run-table clock as China Standard Time and return UTC."""
    return pd.Timestamp(text).tz_localize(LOG_TZ).tz_convert("UTC")


def load_run_folder(path: str | Path) -> tuple[list[Alert], list[Fault]]:
    files = sorted(Path(path).glob("run_table_*.csv"))
    if not files:
        raise FileNotFoundError(f"No run_table_*.csv found in {path}")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    return parse_run_table(df)


def parse_run_table(df: pd.DataFrame) -> tuple[list[Alert], list[Fault]]:
    alerts: list[Alert] = []
    faults: list[Fault] = []
    fault_counter = 0

    for i, row in df.iterrows():
        message = str(row["message"]).strip()
        service = str(row["service"])
        header = LOG_HEADER_RE.match(message)
        if header is None:
            continue  # malformed line: skip (design spec 7: flagged, not guessed)
        ts = _log_time_to_utc(header.group("ts"))
        level = header.group("level").lower()

        injection = INJECTION_RE.search(message)
        if injection:
            fault_counter += 1
            start = _log_time_to_utc(injection.group("start"))
            end = start + pd.Timedelta(seconds=int(injection.group("duration")))
            faults.append(
                Fault(
                    fault_id=f"f{fault_counter:04d}",
                    start=start,
                    end=end,
                    component=service,
                    description=injection.group("tag"),
                )
            )
            continue

        alerts.append(
            Alert(
                alert_id=f"a{i:06d}",
                timestamp=ts,
                source=service,
                alert_type=level,
                severity=level,
                message=message[:300],
            )
        )

    _label_alerts(alerts, faults)
    alerts.sort(key=lambda a: a.timestamp)
    faults.sort(key=lambda f: f.start)
    return alerts, faults


def _label_alerts(alerts: list[Alert], faults: list[Fault]) -> None:
    """Label each alert with the fault whose window covers it, same service."""
    by_service: dict[str, list[Fault]] = {}
    for fault in faults:
        by_service.setdefault(fault.component, []).append(fault)
    for faults_for_service in by_service.values():
        faults_for_service.sort(key=lambda f: f.start)
    for alert in alerts:
        for fault in by_service.get(alert.source, []):
            if fault.start <= alert.timestamp <= fault.end:
                alert.fault_id = fault.fault_id
                break
            if fault.start > alert.timestamp:
                break
