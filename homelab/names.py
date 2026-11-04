"""Map a pod name back to the demo's service name.

Ground truth, metrics and alerts use `company/service`. Replica suffixes and
Helm release prefixes must not leak into that service name, or a fault on
`frontend` would miss a series called `borealis-frontend`.
"""
from __future__ import annotations

import re

_REPLICA = re.compile(r"-[a-z0-9]{5,10}-[a-z0-9]{5}$")
_INDEX = re.compile(r"-[0-9]+$")


def service_from_pod(pod: str, catalog: list[str]) -> str:
    base = _REPLICA.sub("", pod.strip())
    base = _INDEX.sub("", base)
    if base in catalog:
        return base
    for name in sorted(catalog, key=len, reverse=True):
        if base == name or base.endswith("-" + name):
            return name
    return base


def tenant_source(company: str, service: str) -> str:
    service = service.strip().strip("/")
    prefix = f"{company}/"
    if service.startswith(prefix):
        return service
    return f"{company}/{service}"
