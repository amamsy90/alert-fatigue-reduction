"""Persisted ops overrides: shadow/live mode and PagerDuty routing key.

Env vars are the bootstrap default when no override is saved. Secrets in the
ops file are never returned in full by the API (configured bool + last4 only).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

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
