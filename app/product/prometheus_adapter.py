"""Fetch container memory series from Prometheus into the spike DataFrame shape."""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import ssl
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, getproxies, proxy_bypass

import pandas as pd

DEFAULT_QUERY = 'container_memory_working_set_bytes{container!="", container!="POD"}'
MAX_POINTS = 10_000
_POD_SUFFIX_RE = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[a-z0-9]{5}$")


class _RefuseRedirect(HTTPRedirectHandler):
    """A checked Prometheus URL must not bounce the request somewhere unchecked."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Prometheus redirects are refused (SSRF)", headers, fp)


class _PinnedHTTP(http.client.HTTPConnection):
    """Dial the IP the safety check approved, with the original host in Host."""

    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip
        self._ssl_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(sock, server_hostname=self.host)


class _BoundResponse:
    """Response plus the connection it came from, so `with urlopen` closes both."""

    def __init__(self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse) -> None:
        self._conn = conn
        self._resp = resp
        self.status = resp.status

    def read(self) -> bytes:
        return self._resp.read()

    def __enter__(self) -> _BoundResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._resp.close()
        self._conn.close()


def _host_header(host: str, port: int, scheme: str) -> str:
    name = f"[{host}]" if ":" in host else host
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return name
    return f"{name}:{port}"


def _proxy_for(scheme: str, host: str) -> tuple[str, int] | None:
    """(host, port) of the HTTP(S)_PROXY for this scheme, unless NO_PROXY covers the host."""
    raw = getproxies().get(scheme)
    if not raw or proxy_bypass(host):
        return None
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or 80


def urlopen(url: str, timeout: float | None = None):
    """Open a Prometheus URL. Dials the IP from the safety check, or the configured
    HTTP(S)_PROXY; does not follow redirects."""
    from product.ops import pinned_prom_address

    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    ip = pinned_prom_address(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    timeout = 60.0 if timeout is None else timeout
    conn: http.client.HTTPConnection
    target = path
    proxy = _proxy_for(scheme, host)
    if proxy is not None:
        # The operator's proxy resolves and dials the host. The safety check above
        # still ran on the URL, and the proxy address itself is operator config.
        proxy_host, proxy_port = proxy
        if scheme == "https":
            conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=timeout, context=ssl.create_default_context())
            conn.set_tunnel(host, port)
        else:
            conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=timeout)
            target = f"{scheme}://{_host_header(host, port, scheme)}{path}"
    elif scheme == "https":
        conn = _PinnedHTTPS(host, port, ip, timeout, ssl.create_default_context())
    else:
        conn = _PinnedHTTP(host, port, ip, timeout)
    try:
        conn.request("GET", target, headers={"Host": _host_header(host, port, scheme), "Accept": "application/json"})
        resp = conn.getresponse()
    except Exception:
        conn.close()
        raise
    if resp.status >= 300:
        code = resp.status
        payload = BytesIO(resp.read())
        resp.close()
        conn.close()
        reason = "Prometheus redirects are refused (SSRF)" if code < 400 else "Prometheus returned an error"
        raise HTTPError(url, code, reason, Message(), payload)
    return _BoundResponse(conn, resp)


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
    """Explicit arg, then saved ops.json, then PROM_URL env. SSRF-checked."""
    resolved = ""
    if base_url:
        resolved = str(base_url).rstrip("/")
    else:
        try:
            from product.ops import get_prom_url

            saved = get_prom_url()
            if saved:
                resolved = saved
        except Exception:  # noqa: BLE001 — ops may be unavailable in unit isolation
            pass
        if not resolved:
            resolved = (os.environ.get("PROM_URL") or "").rstrip("/")
    if resolved:
        from product.ops import assert_prom_url_safe

        assert_prom_url_safe(resolved)
    return resolved


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

    The whole window is read at `step` seconds, in as many queries as
    Prometheus' point limit needs. One query would have to widen the step
    instead: 14 days in one query is a 121 s step, and the jump rule links hot
    points at most `memory_gap_minutes` (2) apart, so it could never confirm a
    jump on live data. The chunks share one time grid and do not overlap, so a
    point is never read twice.
    """
    base_url = _resolve_prom_url(base_url)
    if not base_url:
        raise ValueError("PROM_URL is not set")
    query = query or os.environ.get("PROM_QUERY") or DEFAULT_QUERY
    end = datetime.now(timezone.utc).timestamp()
    start = end - hours * 3600
    step = max(1.0, float(step))
    span = (MAX_POINTS - 1) * step

    rows: list[dict] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + span)
        params = urlencode({"query": query, "start": chunk_start, "end": chunk_end, "step": step})
        url = f"{base_url}/api/v1/query_range?{params}"
        with urlopen(url, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {payload}")
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
        chunk_start = chunk_end + step
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
    try:
        base_url = _resolve_prom_url(base_url)
    except ValueError as exc:
        return {"reachable": False, "error": str(exc)}
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
    try:
        base_url = _resolve_prom_url(base_url)
    except ValueError as exc:
        raw = (base_url or "").strip() if base_url else ""
        return {
            "ok": False,
            "series_count": 0,
            "points": 0,
            "error": str(exc),
            "url_host": prom_url_host(raw) if raw else None,
        }
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
