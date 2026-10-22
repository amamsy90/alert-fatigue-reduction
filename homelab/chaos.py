"""Chaos Mesh experiments and the ground-truth rows they produce.

One company has at most one open fault. The gap between starts is drawn from
that company's 20–60 minute window. Pod kill is recorded as a two-minute
window because the process is only down until Kubernetes restarts it; the
scheduler still waits the full gap before the next fault.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .catalog import App
from .companies import Company
from .names import tenant_source

POD_KILL_WINDOW = timedelta(minutes=2)
EXPERIMENT_NAME = "homelab"


@dataclass(frozen=True)
class Choice:
    fault_type: str
    service: str
    duration: timedelta
    recorded: timedelta


@dataclass(frozen=True)
class FaultRow:
    fault_id: str
    company: str
    service: str
    fault_type: str
    start: datetime
    end: datetime
    status: str

    def as_dict(self) -> dict[str, str]:
        return {
            "fault_id": self.fault_id,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "component": tenant_source(self.company, self.service),
            "description": f"{self.fault_type} on {self.service} ({self.status})",
            "company": self.company,
            "fault_type": self.fault_type,
            "service": self.service,
            "status": self.status,
        }


def _iso(stamp: datetime) -> str:
    value = stamp.astimezone(timezone.utc).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def choose(company: Company, app: App, rng: random.Random) -> Choice | None:
    types = list(company.chaos.types)
    rng.shuffle(types)
    for fault_type in types:
        names = app.names()
        if fault_type == "http-abort":
            names = [name for name in names if app.http_port(name)]
        if not names:
            continue
        service = rng.choice(names)
        minutes = rng.randint(company.chaos.duration_min_minutes, company.chaos.duration_max_minutes)
        duration = timedelta(minutes=minutes)
        recorded = POD_KILL_WINDOW if fault_type == "pod-kill" else duration
        return Choice(fault_type, service, duration, recorded)
    return None


def gap_minutes(company: Company, rng: random.Random) -> int:
    return rng.randint(company.chaos.interval_min_minutes, company.chaos.interval_max_minutes)


def fault_id(company: str, service: str, fault_type: str, start: datetime) -> str:
    stamp = _iso(start).replace("-", "").replace(":", "")
    return f"{company}-{stamp}-{fault_type}-{service}"


def manifest(company: Company, choice: Choice, app: App) -> str:
    duration = _minutes(choice.duration)
    labels = f"company: {company.id}\n      service: {choice.service}"
    selector = f"""\
  selector:
    namespaces:
      - {company.namespace}
    labelSelectors:
      {labels}
  mode: one
  duration: "{duration}\""""
    kind, body = _body(choice, app, selector)
    return (
        "apiVersion: chaos-mesh.org/v1alpha1\n"
        f"kind: {kind}\n"
        "metadata:\n"
        f"  name: {EXPERIMENT_NAME}\n"
        f"  namespace: {company.namespace}\n"
        "  labels:\n"
        f"    company: {company.id}\n"
        f"    homelab: fault\n"
        "spec:\n"
        f"{body}\n"
    )


def _minutes(duration: timedelta) -> str:
    seconds = int(duration.total_seconds())
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _body(choice: Choice, app: App, selector: str) -> tuple[str, str]:
    if choice.fault_type == "pod-kill":
        return "PodChaos", f"  action: pod-kill\n{selector}\n"
    if choice.fault_type == "network-delay":
        return "NetworkChaos", (
            "  action: delay\n"
            f"{selector}\n"
            "  delay:\n"
            '    latency: "300ms"\n'
            '    jitter: "50ms"\n'
            '    correlation: "25"\n'
        )
    if choice.fault_type == "network-loss":
        return "NetworkChaos", (
            "  action: loss\n"
            f"{selector}\n"
            "  loss:\n"
            '    loss: "20"\n'
            '    correlation: "25"\n'
        )
    if choice.fault_type == "cpu-stress":
        return "StressChaos", (
            f"{selector}\n"
            "  stressors:\n"
            "    cpu:\n"
            "      workers: 1\n"
            "      load: 70\n"
        )
    if choice.fault_type == "memory-stress":
        return "StressChaos", (
            f"{selector}\n"
            "  stressors:\n"
            "    memory:\n"
            "      workers: 1\n"
            '      size: "256MB"\n'
        )
    port = app.http_port(choice.service)
    if choice.fault_type == "http-abort" and port:
        return "HTTPChaos", (
            "  target: Request\n"
            f"{selector}\n"
            f"  port: {port}\n"
            '  path: "*"\n'
            "  abort: true\n"
        )
    raise ValueError(f"cannot render {choice.fault_type}")


FAULT_COLUMNS = (
    "fault_id",
    "start",
    "end",
    "component",
    "description",
    "company",
    "fault_type",
    "service",
    "status",
)
