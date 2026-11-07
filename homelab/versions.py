"""Pins in homelab/config/versions.env. Scripts source the same file."""
from __future__ import annotations

from pathlib import Path

REQUIRED = (
    "KPS_CHART_VERSION",
    "CHAOS_CHART_VERSION",
    "OTEL_DEMO_CHART_VERSION",
    "BOUTIQUE_REF",
    "SOCK_SHOP_REF",
    "LOKI_IMAGE",
    "TEMPO_IMAGE",
    "COLLECTOR_IMAGE",
    "ALLOY_IMAGE",
    "CURL_IMAGE",
    "CLUSTER_NAME",
)

DEFAULT_PATH = Path(__file__).resolve().parent / "config" / "versions.env"


def load_versions(path: Path | None = None) -> dict[str, str]:
    file = path or DEFAULT_PATH
    found: dict[str, str] = {}
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    missing = [key for key in REQUIRED if not found.get(key)]
    if missing:
        raise ValueError(f"{file} is missing {', '.join(missing)}")
    return found
