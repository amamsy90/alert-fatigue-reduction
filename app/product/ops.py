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
                "Live allows pages on ingest jobs when at least one channel is set."
            )
        if not configured_channels():
            raise ValueError(
                "Turning Live on requires a channel first: a PagerDuty routing key, "
                "a Slack webhook or an Opsgenie key (save one on the Connectors panel "
                "or set PAGERDUTY_ROUTING_KEY / SLACK_WEBHOOK_URL / OPSGENIE_API_KEY)."
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
    data = _load()
    data[name] = value
    _save(data)
    return _secret_status(name)


def _clear_secret(name: str) -> dict:
    data = _load()
    data.pop(name, None)
    _save(data)
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
    if url and not url.startswith("https://"):
        raise ValueError("Slack webhook must be an https:// URL")
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


def can_page() -> bool:
    """Live mode and at least one channel (PagerDuty, Slack, Opsgenie) are required."""
    return (not get_shadow_mode()) and bool(configured_channels())


# Modes that may call notify_escalations when Live + key. Sample, GAIA,
# Evaluate/upload demos must never page even if Live is on.
PAGEABLE_MODES = frozenset({"ingest"})


def may_page_for_mode(mode: str) -> bool:
    return mode in PAGEABLE_MODES and can_page()


# --- Paging watermark -------------------------------------------------------
# An ingest job re-scores up to 14 days of metrics. Without a watermark, the
# first Live run would page every jump shown in that history. Only metric items
# confirmed after the last ingest job (or, the first time, within the last
# PAGE_LOOKBACK_MINUTES) are paged or counted as would-have-paged.

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
    data = _load()
    data["paged_through"] = ts.isoformat()
    _save(data)


def clear_page_watermark() -> None:
    data = _load()
    data.pop("paged_through", None)
    _save(data)


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
        "note": "PROM_QUERY and PROM_SERVICE_LABEL are env-only; URL may be saved here or set via PROM_URL.",
    }
