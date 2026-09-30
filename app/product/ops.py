"""Persisted ops overrides: shadow/live mode, PagerDuty routing key, Prometheus URL.

Env vars are the bootstrap default when no override is saved. Secrets in the
ops file are never returned in full by the API (configured bool + last4 only).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_OPS_PATH = Path(__file__).resolve().parent / "data" / "ops.json"

# Cloud metadata / link-local names that must never be reachable via PROM_URL,
# even when PROM_ALLOW_PRIVATE=1 (SSRF).
_BLOCKED_PROM_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)
_PRIVATE_PROM_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


def ops_path() -> Path:
    return Path(os.environ.get("OPS_PATH") or DEFAULT_OPS_PATH)


def _load() -> dict:
    path = ops_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> dict:
    path = ops_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name per write, so two writers never share one file.
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(data, indent=2))
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # Secrets live here (routing keys, Slack webhook, Opsgenie key). Best-effort
    # owner-only mode on Unix; Windows may ignore or raise — never fail the save.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data


_LOCK = threading.RLock()


@contextmanager
def _edit() -> Iterator[dict]:
    """Read, change and write ops.json as one step. The UI (a key or the Live
    switch) and a scoring job (the page watermark) write from different threads;
    without the lock one of them could overwrite the other's change."""
    with _LOCK:
        data = _load()
        yield data
        _save(data)


def env_shadow_mode() -> bool:
    """Bootstrap default from SHADOW_MODE (1/unset = shadow on)."""
    return os.environ.get("SHADOW_MODE", "1") != "0"


def get_shadow_mode() -> bool:
    """Effective shadow mode: saved override, else SHADOW_MODE env."""
    data = _load()
    if "shadow_mode" in data:
        return bool(data["shadow_mode"])
    return env_shadow_mode()


def set_shadow_mode(*, shadow: bool, confirm: str | None = None) -> dict:
    """Persist shadow/live. Turning Live on requires confirm == 'LIVE' and a key."""
    if not shadow:
        if (confirm or "").strip() != "LIVE":
            raise ValueError(
                "Turning Live on requires confirm='LIVE' (typed exactly). "
                "Live allows pages on ingest jobs when at least one channel is set."
            )
        if not configured_channels():
            raise ValueError(
                "Turning Live on requires a channel first: a PagerDuty routing key, "
                "a Slack webhook or an Opsgenie key (save one on the Connectors panel "
                "or set PAGERDUTY_ROUTING_KEY / SLACK_WEBHOOK_URL / OPSGENIE_API_KEY)."
            )
    with _edit() as data:
        data["shadow_mode"] = bool(shadow)
    return shadow_status()


def clear_shadow_override() -> dict:
    """Drop the override so SHADOW_MODE env is used again."""
    with _edit() as data:
        data.pop("shadow_mode", None)
    return shadow_status()


def shadow_status() -> dict:
    data = _load()
    override = data["shadow_mode"] if "shadow_mode" in data else None
    effective = get_shadow_mode()
    return {
        "shadow_mode": effective,
        "live": not effective,
        "override": None if override is None else bool(override),
        "env_default": env_shadow_mode(),
    }


def env_routing_key() -> str:
    return (os.environ.get("PAGERDUTY_ROUTING_KEY") or "").strip()


def get_routing_key() -> str:
    """Effective key: saved ops key first, else PAGERDUTY_ROUTING_KEY env."""
    data = _load()
    saved = (data.get("pagerduty_routing_key") or "").strip()
    if saved:
        return saved
    return env_routing_key()


def set_routing_key(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise ValueError("routing_key must be non-empty; use clear to remove it")
    with _edit() as data:
        data["pagerduty_routing_key"] = key
    return routing_key_status()


def clear_routing_key() -> dict:
    """Remove the saved key (env key still applies if set)."""
    with _edit() as data:
        data.pop("pagerduty_routing_key", None)
    return routing_key_status()


def routing_key_status() -> dict:
    data = _load()
    saved = (data.get("pagerduty_routing_key") or "").strip()
    env = env_routing_key()
    effective = saved or env
    source = "ops" if saved else ("env" if env else None)
    return {
        "configured": bool(effective),
        "last4": effective[-4:] if len(effective) >= 4 else (effective or None),
        "source": source,
    }


# --- Slack and Opsgenie ------------------------------------------------------
# Same pattern as the PagerDuty key: saved in ops.json first, else the env var.

_SECRETS = {
    "slack_webhook": "SLACK_WEBHOOK_URL",
    "opsgenie_api_key": "OPSGENIE_API_KEY",
}


def _get_secret(name: str) -> str:
    saved = (_load().get(name) or "").strip()
    return saved or (os.environ.get(_SECRETS[name]) or "").strip()


def _set_secret(name: str, value: str) -> dict:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{name} must be non-empty; use clear to remove it")
    with _edit() as data:
        data[name] = value
    return _secret_status(name)


def _clear_secret(name: str) -> dict:
    with _edit() as data:
        data.pop(name, None)
    return _secret_status(name)


def _secret_status(name: str) -> dict:
    saved = (_load().get(name) or "").strip()
    env = (os.environ.get(_SECRETS[name]) or "").strip()
    effective = saved or env
    return {
        "configured": bool(effective),
        "last4": effective[-4:] if len(effective) >= 4 else (effective or None),
        "source": "ops" if saved else ("env" if env else None),
    }


def get_slack_webhook() -> str:
    return _get_secret("slack_webhook")


def set_slack_webhook(url: str) -> dict:
    url = (url or "").strip()
    if url and not url.startswith("https://hooks.slack.com/"):
        raise ValueError(
            "Slack webhook must be an https://hooks.slack.com/... URL "
            "(incoming webhook). Set SLACK_WEBHOOK_URL in the environment "
            "only if you use a non-Slack gateway."
        )
    return _set_secret("slack_webhook", url)


def clear_slack_webhook() -> dict:
    return _clear_secret("slack_webhook")


def slack_status() -> dict:
    return _secret_status("slack_webhook")


def get_opsgenie_key() -> str:
    return _get_secret("opsgenie_api_key")


def set_opsgenie_key(key: str) -> dict:
    return _set_secret("opsgenie_api_key", key)


def clear_opsgenie_key() -> dict:
    return _clear_secret("opsgenie_api_key")


def opsgenie_status() -> dict:
    return {**_secret_status("opsgenie_api_key"), "api_url": opsgenie_api_url()}


def opsgenie_api_url() -> str:
    """OPSGENIE_API_URL for the EU region (https://api.eu.opsgenie.com); US by default."""
    return (os.environ.get("OPSGENIE_API_URL") or "https://api.opsgenie.com").rstrip("/")


def configured_channels() -> list[str]:
    out = []
    if get_routing_key():
        out.append("pagerduty")
    if get_slack_webhook():
        out.append("slack")
    if get_opsgenie_key():
        out.append("opsgenie")
    return out


def login_configured() -> bool:
    """A login password is set (APP_PASSWORD)."""
    return bool(os.environ.get("APP_PASSWORD"))


def can_page() -> bool:
    """Live mode, at least one channel (PagerDuty, Slack, Opsgenie) and a login
    password are all required. The password check is here as well as on the
    Live switch, so SHADOW_MODE=0 in the environment cannot bypass it."""
    return (not get_shadow_mode()) and bool(configured_channels()) and login_configured()


# Modes that may call notify_escalations when Live + key. Sample, GAIA,
# Evaluate/upload demos must never page even if Live is on.
PAGEABLE_MODES = frozenset({"ingest"})


def may_page_for_mode(mode: str) -> bool:
    return mode in PAGEABLE_MODES and can_page()


# --- Paging watermark -------------------------------------------------------
# An ingest job re-scores up to 14 days of metrics. The first ingest job pages
# only items inside PAGE_LOOKBACK_MINUTES and remembers every shown key. Later
# jobs page any key that was not remembered, including a point whose timestamp
# is older than the previous job's clock.

DEFAULT_PAGE_LOOKBACK_MINUTES = 60.0


def page_lookback_minutes() -> float:
    try:
        return max(0.0, float(os.environ.get("PAGE_LOOKBACK_MINUTES", DEFAULT_PAGE_LOOKBACK_MINUTES)))
    except ValueError:
        return DEFAULT_PAGE_LOOKBACK_MINUTES


def get_page_watermark():
    """UTC time up to which metric items were already considered for paging."""
    import pandas as pd

    raw = _load().get("paged_through")
    if not raw:
        return None
    try:
        ts = pd.Timestamp(raw)
    except (ValueError, TypeError):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def page_cutoff(now=None):
    """Metric items shown at or before this time are never paged again."""
    import pandas as pd

    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    mark = get_page_watermark()
    if mark is not None:
        return mark
    return now - pd.Timedelta(minutes=page_lookback_minutes())


def set_page_watermark(ts) -> None:
    with _edit() as data:
        data["paged_through"] = ts.isoformat()


def clear_page_watermark() -> None:
    with _edit() as data:
        data.pop("paged_through", None)
        data.pop("paged_through_by_channel", None)
        data.pop("paged_item_keys", None)
        data.pop("paged_item_keys_by_channel", None)


def seen_spike_keys() -> set[str] | None:
    """Keys already considered for paging, or None before the first ingest job.

    A missing key is a new item even when its timestamp is older than the last
    job's clock (a Prometheus point that arrived late).
    """
    data = _load()
    if "paged_item_keys" not in data:
        return None
    return set(data.get("paged_item_keys") or [])


def set_seen_spike_keys(keys: set[str]) -> None:
    with _edit() as data:
        data["paged_item_keys"] = sorted(keys)


def channel_delivered_keys(channel: str) -> set[str]:
    raw = (_load().get("paged_item_keys_by_channel") or {}).get(channel) or []
    return set(raw)


def add_channel_delivered_keys(channel: str, keys: set[str]) -> None:
    if not keys:
        return
    with _edit() as data:
        marks = data.setdefault("paged_item_keys_by_channel", {})
        current = set(marks.get(channel) or [])
        marks[channel] = sorted(current | keys)


def channel_cutoff(channel: str, now=None):
    """Like page_cutoff, but a channel that already delivered past the shared
    mark (while another channel failed) keeps its own, later mark, so the retry
    pages only the channel that failed."""
    import pandas as pd

    cutoff = page_cutoff(now)
    raw = (_load().get("paged_through_by_channel") or {}).get(channel)
    if raw:
        try:
            ts = pd.Timestamp(raw)
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
            cutoff = max(cutoff, ts)
        except (ValueError, TypeError):
            pass
    return cutoff


def set_channel_watermark(channel: str, ts) -> None:
    with _edit() as data:
        marks = data.setdefault("paged_through_by_channel", {})
        marks[channel] = ts.isoformat()


def env_prom_url() -> str:
    return (os.environ.get("PROM_URL") or "").strip().rstrip("/")


def get_prom_url() -> str:
    """Effective Prometheus base URL: saved ops first, else PROM_URL env."""
    data = _load()
    saved = (data.get("prometheus_url") or "").strip().rstrip("/")
    if saved:
        return saved
    return env_prom_url()


def prom_allow_private() -> bool:
    """Loopback / RFC1918 Prometheus targets need PROM_ALLOW_PRIVATE=1 (pilot default off)."""
    return os.environ.get("PROM_ALLOW_PRIVATE", "0") == "1"


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _host_addresses(host: str) -> list[IPAddress]:
    """Every address a Prometheus host means: IP literals (including legacy forms
    such as 127.1, 2130706433 or 0x7f000001) and whatever DNS returns.

    An empty list means the name does not resolve. Callers must refuse that:
    a later lookup could return a private address.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    if re.fullmatch(r"[0-9a-fx.]+", host):
        try:
            return [ipaddress.ip_address(socket.inet_aton(host))]
        except OSError:
            pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return []
    out: list[IPAddress] = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]  # drop an IPv6 zone id
        try:
            out.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return out


def _private_refused() -> ValueError:
    return ValueError(
        "Private/loopback Prometheus URLs need PROM_ALLOW_PRIVATE=1 "
        "(safe default blocks them to reduce SSRF risk)."
    )


def assert_prom_url_safe(url: str) -> None:
    """Refuse SSRF-prone Prometheus targets (metadata / link-local; private unless allowed).

    The host is resolved and every address it maps to is checked, so numeric
    tricks (127.1, 2130706433), a trailing dot, or a public name that points at
    a private or metadata address are refused too. Set PROM_ALLOW_PRIVATE=1 for
    in-cluster / Docker Compose Prometheus. Cloud metadata hosts and link-local
    addresses are always refused. The fetch dials the address returned by this
    lookup (it does not resolve the name again) and refuses redirects.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("prometheus_url must be non-empty")
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("prometheus_url must be an http(s) URL, e.g. http://localhost:9090")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("prometheus_url must include a host")
    if host in _BLOCKED_PROM_HOSTS or host.endswith(".metadata.google.internal"):
        raise ValueError("Prometheus URL host is not allowed (cloud metadata)")
    if host in _PRIVATE_PROM_HOSTNAMES or host.endswith((".localhost", ".local")):
        if not prom_allow_private():
            raise _private_refused()
    addresses = _host_addresses(host)
    if not addresses:
        raise ValueError(
            "Prometheus host did not resolve; refusing it so a later lookup cannot reach a private address"
        )
    approved: list[IPAddress] = []
    for ip in addresses:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if ip.is_loopback:
            if not prom_allow_private():
                raise _private_refused()
            continue
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError(
                "Prometheus URL must not target link-local, multicast, reserved or unspecified addresses"
            )
        if (ip.is_private or not ip.is_global) and not prom_allow_private():
            raise _private_refused()
        approved.append(ip)
    # Loopback was skipped above after the allow-private check. Dial one of the
    # addresses this lookup returned; the fetch must not resolve the name again.
    chosen = approved[0] if approved else addresses[0]
    _APPROVED_PROM[(_pin_key(url))] = str(chosen)


# Base URL -> IP approved by the lookup in assert_prom_url_safe. The fetch dials
# this address instead of resolving the name a second time (DNS rebinding).
_APPROVED_PROM: dict[str, str] = {}


def _pin_key(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def pinned_prom_address(url: str) -> str:
    """IP from the safety check for this URL, resolving once if it has not run yet."""
    key = _pin_key(url)
    ip = _APPROVED_PROM.get(key)
    if ip:
        return ip
    assert_prom_url_safe(url if "://" in url else f"http://{url}")
    return _APPROVED_PROM[key]


def set_prom_url(url: str) -> dict:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("prometheus_url must be non-empty; use clear to remove it")
    # Light validation — must look like an HTTP(S) base, no query string required
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("prometheus_url must be an http(s) URL, e.g. http://localhost:9090")
    if "://" not in url:
        url = f"http://{url}"
    assert_prom_url_safe(url)
    with _edit() as data:
        data["prometheus_url"] = url.rstrip("/")
    return prom_url_status()


def clear_prom_url() -> dict:
    """Remove the saved Prometheus URL (env PROM_URL still applies if set)."""
    with _edit() as data:
        data.pop("prometheus_url", None)
    return prom_url_status()


def redact_url(url: str) -> str:
    """Drop any user:password@ part so a URL is safe to show or return."""
    if not url:
        return url
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if not (parsed.username or parsed.password):
        return url
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=f"***@{netloc}").geturl()


def prom_url_status() -> dict:
    data = _load()
    saved = (data.get("prometheus_url") or "").strip().rstrip("/")
    env = env_prom_url()
    effective = saved or env
    source = "ops" if saved else ("env" if env else None)
    host = None
    if effective:
        try:
            parsed = urlparse(effective if "://" in effective else f"http://{effective}")
            host = parsed.hostname
        except Exception:  # noqa: BLE001
            host = None
    return {
        "configured": bool(effective),
        "url": redact_url(effective) or None,
        "url_host": host,
        "source": source,
        "query_set": bool(os.environ.get("PROM_QUERY")),
        "service_label": os.environ.get("PROM_SERVICE_LABEL") or None,
        "allow_private": prom_allow_private(),
        "note": "PROM_QUERY and PROM_SERVICE_LABEL are env-only; URL may be saved here or set via PROM_URL. "
        "Private/loopback targets need PROM_ALLOW_PRIVATE=1; cloud metadata is always refused.",
    }
