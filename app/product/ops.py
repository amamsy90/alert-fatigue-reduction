"""Persisted ops overrides: shadow/live mode, PagerDuty routing key, Prometheus URL.

Env vars are the bootstrap default when no override is saved. Secrets in the
ops file are never returned in full by the API (configured bool + last4 only).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_OPS_PATH = Path(__file__).resolve().parent / "data" / "ops.json"


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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    return data


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
                "Live allows PagerDuty pages on ingest jobs when a routing key is set."
            )
        if not get_routing_key():
            raise ValueError(
                "Turning Live on requires a routing key first "
                "(save one via POST /ops/pagerduty/key or set PAGERDUTY_ROUTING_KEY)."
            )
    data = _load()
    data["shadow_mode"] = bool(shadow)
    _save(data)
    return shadow_status()


def clear_shadow_override() -> dict:
    """Drop the override so SHADOW_MODE env is used again."""
    data = _load()
    data.pop("shadow_mode", None)
    _save(data)
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
    data = _load()
    data["pagerduty_routing_key"] = key
    _save(data)
    return routing_key_status()


def clear_routing_key() -> dict:
    """Remove the saved key (env key still applies if set)."""
    data = _load()
    data.pop("pagerduty_routing_key", None)
    _save(data)
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


def can_page() -> bool:
    """Live mode and a routing key are both required before any page is sent."""
    return (not get_shadow_mode()) and bool(get_routing_key())


# Modes that may call notify_escalations when Live + key. Sample, GAIA,
# Evaluate/upload demos must never page even if Live is on.
PAGEABLE_MODES = frozenset({"ingest"})


def may_page_for_mode(mode: str) -> bool:
    return mode in PAGEABLE_MODES and can_page()


def env_prom_url() -> str:
    return (os.environ.get("PROM_URL") or "").strip().rstrip("/")


def get_prom_url() -> str:
    """Effective Prometheus base URL: saved ops first, else PROM_URL env."""
    data = _load()
    saved = (data.get("prometheus_url") or "").strip().rstrip("/")
    if saved:
        return saved
    return env_prom_url()


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
    data = _load()
    data["prometheus_url"] = url.rstrip("/")
    _save(data)
    return prom_url_status()


def clear_prom_url() -> dict:
    """Remove the saved Prometheus URL (env PROM_URL still applies if set)."""
    data = _load()
    data.pop("prometheus_url", None)
    _save(data)
    return prom_url_status()


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
        "url": effective or None,
        "url_host": host,
        "source": source,
        "query_set": bool(os.environ.get("PROM_QUERY")),
        "service_label": os.environ.get("PROM_SERVICE_LABEL") or None,
        "note": "PROM_QUERY and PROM_SERVICE_LABEL are env-only; URL may be saved here or set via PROM_URL.",
    }
