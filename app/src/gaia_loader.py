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
from bisect import bisect_right
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
    folder = Path(path)
    files = sorted([*folder.glob("run_table_*.csv"), *folder.glob("run_table_*.csv.gz")])
    if not files:
        raise FileNotFoundError(f"No run_table_*.csv or run_table_*.csv.gz found in {path}")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    return parse_run_table(df)


def parse_run_table(df: pd.DataFrame) -> tuple[list[Alert], list[Fault]]:
    """Vectorised: one regex pass per column, one timezone conversion per column."""
    message = df["message"].astype(str).str.strip()
    header = message.str.extract(LOG_HEADER_RE)
    ok = header["ts"].notna()
    df, message, header = df[ok], message[ok], header[ok]
    ts = pd.to_datetime(header["ts"]).dt.tz_localize(LOG_TZ).dt.tz_convert("UTC")
    level = header["level"].str.lower()
    service = df["service"].astype(str)
    injection = message.str.extract(INJECTION_RE)
    is_fault = injection["start"].notna()

    fault_rows = injection[is_fault]
    starts = pd.to_datetime(fault_rows["start"], format="mixed").dt.tz_localize(LOG_TZ).dt.tz_convert("UTC")
    faults = [
        Fault(
            fault_id=f"f{n:04d}",
            start=start,
            end=start + pd.Timedelta(seconds=int(duration)),
            component=svc,
            description=tag,
        )
        for n, (start, duration, svc, tag) in enumerate(
            zip(starts, fault_rows["duration"], service[is_fault], fault_rows["tag"]), start=1
        )
    ]
    keep = ~is_fault
    alerts = [
        Alert(
            alert_id=f"a{i:06d}",
            timestamp=t,
            source=svc,
            alert_type=lvl,
            severity=lvl,
            message=msg[:300],
        )
        for i, t, svc, lvl, msg in zip(
            df.index[keep], ts[keep], service[keep], level[keep], message[keep]
        )
    ]
    _label_alerts(alerts, faults)
    alerts.sort(key=lambda a: a.timestamp)
    faults.sort(key=lambda f: f.start)
    return alerts, faults


def _label_alerts(alerts: list[Alert], faults: list[Fault]) -> None:
    """Label each alert with the fault whose window covers it, same service.

    Sorted sweep per service (O(n log n)); when windows overlap, the earliest
    starting fault that still covers the alert wins, as before.
    """
    by_service: dict[str, list[Fault]] = {}
    for fault in faults:
        by_service.setdefault(fault.component, []).append(fault)
    alerts_by_service: dict[str, list[Alert]] = {}
    for alert in alerts:
        alerts_by_service.setdefault(alert.source, []).append(alert)
    for service, service_alerts in alerts_by_service.items():
        service_faults = sorted(by_service.get(service, []), key=lambda f: f.start)
        if not service_faults:
            continue
        starts = [f.start for f in service_faults]
        longest = max(f.end - f.start for f in service_faults)
        for alert in service_alerts:
            hi = bisect_right(starts, alert.timestamp)
            match = None
            for idx in range(hi - 1, -1, -1):
                fault = service_faults[idx]
                if fault.start < alert.timestamp - longest:
                    break
                if fault.end >= alert.timestamp:
                    match = fault  # keep going back: the earliest covering fault wins
            if match is not None:
                alert.fault_id = match.fault_id
