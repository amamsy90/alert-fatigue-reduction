"""Fetch container memory series from Prometheus into the spike DataFrame shape."""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

import pandas as pd

DEFAULT_QUERY = 'container_memory_working_set_bytes{container!="", container!="POD"}'
MAX_POINTS = 10_000
_POD_SUFFIX_RE = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[a-z0-9]{5}$")


def service_from_labels(metric: dict[str, str], label: str | None = None) -> str:
    """Map a series to the service name alerts use as their `source`.

    Pod names carry replica-set hashes (webservice1-7d9f8c6b5-x2k4p) that never
    match an alert source, so they are stripped when no better label exists.
    """
    label = label or os.environ.get("PROM_SERVICE_LABEL")
    if label and metric.get(label):
        return metric[label]
    for key in ("service", "app", "app_kubernetes_io_name", "container"):
        if metric.get(key):
            return metric[key]
    pod = metric.get("pod") or metric.get("name")
    if pod:
        return _POD_SUFFIX_RE.sub("", pod)
    return metric.get("job") or "unknown"


def step_seconds(hours: float, requested: float) -> int:
    """Prometheus rejects ranges over 11,000 points per series."""
    return int(max(requested, math.ceil(hours * 3600 / MAX_POINTS)))


def _resolve_prom_url(base_url: str | None = None) -> str:
    """Explicit arg, then saved ops.json, then PROM_URL env."""
    if base_url:
        return str(base_url).rstrip("/")
    try:
        from product.ops import get_prom_url

        saved = get_prom_url()
        if saved:
            return saved
    except Exception:  # noqa: BLE001 — ops may be unavailable in unit isolation
        pass
    return (os.environ.get("PROM_URL") or "").rstrip("/")


def prom_url_host(base_url: str) -> str | None:
    """Hostname only — never credentials or full URL."""
    try:
        parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
        return parsed.hostname
    except Exception:  # noqa: BLE001
        return None


def fetch_memory_metrics(
    base_url: str | None = None,
    query: str | None = None,
    hours: float = 24.0 * 14,
    step: float = 60.0,
) -> pd.DataFrame:
    """Query Prometheus HTTP API and return timestamp, value, service, series_id.

    Replicas of one service are summed, so a jump on any replica shows up.
    """
    base_url = _resolve_prom_url(base_url)
    if not base_url:
        raise ValueError("PROM_URL is not set")
    query = query or os.environ.get("PROM_QUERY") or DEFAULT_QUERY
    end = datetime.now(timezone.utc)
    start = end - pd.Timedelta(hours=hours)
    params = urlencode(
        {
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step_seconds(hours, step),
        }
    )
    url = f"{base_url}/api/v1/query_range?{params}"
    with urlopen(url, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")

    rows: list[dict] = []
    for series in payload.get("data", {}).get("result", []):
        service = service_from_labels(series.get("metric", {}))
        for ts, value in series.get("values", []):
            rows.append(
                {
                    "timestamp": pd.to_datetime(float(ts), unit="s", utc=True),
                    "value": float(value),
                    "service": service,
                }
            )
    if not rows:
        return pd.DataFrame(columns=["timestamp", "value", "service", "series_id"])
    frame = (
        pd.DataFrame(rows)
        .groupby(["service", "timestamp"], as_index=False)["value"]
        .sum()
    )
    frame["series_id"] = frame["service"]
    return frame.sort_values("timestamp").reset_index(drop=True)


def probe_prometheus(base_url: str | None = None, timeout: float = 2.0) -> dict:
    """Reach Prometheus `/api/v1/status/buildinfo`. Does not return the URL or secrets."""
    base_url = _resolve_prom_url(base_url)
    if not base_url:
        return {"reachable": False, "error": "PROM_URL is not set"}
    url = f"{base_url}/api/v1/status/buildinfo"
    try:
        with urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — show type only; the URL may contain a password
        return {"reachable": False, "error": type(exc).__name__}
    if payload.get("status") != "success":
        return {"reachable": False, "error": "Prometheus status was not success"}
    version = (payload.get("data") or {}).get("version")
    return {"reachable": True, "version": version}


def run_prometheus_test(
    base_url: str | None = None,
    query: str | None = None,
    hours: float = 2.0,
    timeout: float = 5.0,
) -> dict:
    """Probe reachability and run a short range query; never return huge payloads.

    Returns ``{ok, series_count, points, error?, url_host, version?}``.
    """
    base_url = _resolve_prom_url(base_url)
    host = prom_url_host(base_url) if base_url else None
    if not base_url:
        return {
            "ok": False,
            "series_count": 0,
            "points": 0,
            "error": "PROM_URL is not set",
            "url_host": None,
        }
    probe = probe_prometheus(base_url, timeout=timeout)
    if not probe.get("reachable"):
        return {
            "ok": False,
            "series_count": 0,
            "points": 0,
            "error": probe.get("error") or "unreachable",
            "url_host": host,
        }
    query = query or os.environ.get("PROM_QUERY") or DEFAULT_QUERY
    end = datetime.now(timezone.utc)
    start = end - pd.Timedelta(hours=hours)
    # Wide step keeps the response tiny (a handful of points per series).
    step = step_seconds(hours, max(60.0, hours * 3600 / 20))
    params = urlencode(
        {
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        }
    )
    url = f"{base_url}/api/v1/query_range?{params}"
    try:
        with urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — never echo URL (may hold a password)
        return {
            "ok": False,
            "series_count": 0,
            "points": 0,
            "error": type(exc).__name__,
            "url_host": host,
            "version": probe.get("version"),
        }
    if payload.get("status") != "success":
        return {
            "ok": False,
            "series_count": 0,
            "points": 0,
            "error": "Prometheus query was not success",
            "url_host": host,
            "version": probe.get("version"),
        }
    result = payload.get("data", {}).get("result") or []
    series_count = len(result)
    points = sum(len(s.get("values") or []) for s in result)
    return {
        "ok": True,
        "series_count": series_count,
        "points": points,
        "url_host": host,
        "version": probe.get("version"),
    }
