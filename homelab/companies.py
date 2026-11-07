"""Load homelab/companies.yaml. PyYAML is imported here only."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .catalog import APPS, App

DEFAULT_PATH = Path(__file__).resolve().parent / "companies.yaml"

LOAD_PROFILES = frozenset({"steady", "daily", "bursty"})
SENSITIVITIES = frozenset({"noisy", "strict"})
FAULT_TYPES = frozenset(
    {"pod-kill", "network-delay", "network-loss", "cpu-stress", "memory-stress", "http-abort"}
)


@dataclass(frozen=True)
class ChaosSpec:
    types: tuple[str, ...]
    interval_min_minutes: int
    interval_max_minutes: int
    duration_min_minutes: int
    duration_max_minutes: int


@dataclass(frozen=True)
class Company:
    id: str
    name: str
    enabled: bool
    namespace: str
    app: str
    load_profile: str
    sensitivity: str
    chaos: ChaosSpec
    estimate_ram: str
    estimate_cpu: str
    manifest_url: str = ""
    note: str = ""

    def application(self) -> App | None:
        return APPS.get(self.app)

    def services(self) -> list[str]:
        app = self.application()
        return [] if app is None else app.names()


def _minutes(raw: object, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{field} must be a whole number of minutes")
    if raw < 1:
        raise ValueError(f"{field} must be at least 1 minute")
    return raw


def _company(raw: object) -> Company:
    if not isinstance(raw, dict):
        raise ValueError("each company must be a mapping")
    cid = str(raw.get("id") or "").strip()
    if not cid.replace("-", "").isalnum() or not cid[0].isalpha() or cid != cid.lower():
        raise ValueError(f"company id {cid!r} must be a short lowercase slug")
    app = str(raw.get("app") or "").strip()
    if app not in APPS and app != "manifest":
        raise ValueError(f"{cid}: app must be one of {', '.join(sorted(APPS))} or manifest")
    load = raw.get("load") or {}
    alerts = raw.get("alerts") or {}
    chaos = raw.get("chaos") or {}
    estimate = raw.get("estimate") or {}
    if not isinstance(load, dict) or not isinstance(alerts, dict) or not isinstance(chaos, dict):
        raise ValueError(f"{cid}: load, alerts and chaos must be mappings")
    profile = str(load.get("profile") or "")
    sensitivity = str(alerts.get("sensitivity") or "")
    if profile not in LOAD_PROFILES:
        raise ValueError(f"{cid}: load.profile must be steady, daily or bursty")
    if sensitivity not in SENSITIVITIES:
        raise ValueError(f"{cid}: alerts.sensitivity must be noisy or strict")
    types = chaos.get("types") or []
    if not isinstance(types, list) or not types:
        raise ValueError(f"{cid}: chaos.types must list at least one fault")
    cleaned = tuple(str(item) for item in types)
    unknown = [item for item in cleaned if item not in FAULT_TYPES]
    if unknown:
        raise ValueError(f"{cid}: unknown chaos types {unknown}")
    interval_min = _minutes(chaos.get("interval_min_minutes"), f"{cid} interval_min_minutes")
    interval_max = _minutes(chaos.get("interval_max_minutes"), f"{cid} interval_max_minutes")
    duration_min = _minutes(chaos.get("duration_min_minutes"), f"{cid} duration_min_minutes")
    duration_max = _minutes(chaos.get("duration_max_minutes"), f"{cid} duration_max_minutes")
    if not (20 <= interval_min <= interval_max <= 60):
        raise ValueError(f"{cid}: chaos interval must sit inside 20 to 60 minutes")
    if duration_min > duration_max or duration_max >= interval_min:
        raise ValueError(f"{cid}: fault duration must be shorter than the gap between faults")
    enabled = bool(raw.get("enabled"))
    manifest_url = str(raw.get("manifest_url") or "").strip()
    if enabled and app == "manifest" and not manifest_url:
        raise ValueError(f"{cid}: a manifest company needs manifest_url before it can be enabled")
    namespace = str(raw.get("namespace") or f"company-{cid}")
    return Company(
        id=cid,
        name=str(raw.get("name") or cid),
        enabled=enabled,
        namespace=namespace,
        app=app,
        load_profile=profile,
        sensitivity=sensitivity,
        chaos=ChaosSpec(cleaned, interval_min, interval_max, duration_min, duration_max),
        estimate_ram=str(estimate.get("ram") or ""),
        estimate_cpu=str(estimate.get("cpu") or ""),
        manifest_url=manifest_url,
        note=str(raw.get("note") or "").strip(),
    )


def load_companies(path: Path | None = None) -> list[Company]:
    file = path or DEFAULT_PATH
    raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    rows = raw.get("companies") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{file} needs a companies list")
    companies = [_company(row) for row in rows]
    ids = [company.id for company in companies]
    if len(ids) != len(set(ids)):
        raise ValueError("company ids must be unique")
    return companies


def select_companies(companies: list[Company], only: str | None) -> list[Company]:
    """Enabled companies, optionally narrowed by a comma-separated id list."""
    chosen = [company for company in companies if company.enabled]
    if only:
        wanted = [part.strip() for part in only.split(",") if part.strip()]
        known = {company.id: company for company in companies}
        missing = [item for item in wanted if item not in known]
        if missing:
            raise ValueError(f"unknown company id: {', '.join(missing)}")
        chosen = []
        for item in wanted:
            company = known[item]
            if company.app == "manifest" and not company.manifest_url:
                raise ValueError(f"{company.id} has no manifest_url")
            chosen.append(company)
    if not chosen:
        raise ValueError("no companies selected")
    return chosen
