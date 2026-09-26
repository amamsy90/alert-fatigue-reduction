"""Send escalations to PagerDuty Events API v2. Never used while shadow mode is on."""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from src.entities import Action, Alert, Decision
from src.memory_spikes import Spike

EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
_SEVERITY = {
    "critical": "critical",
    "error": "error",
    "high": "error",
    "warning": "warning",
    "info": "info",
}


def routing_key() -> str:
    return (os.environ.get("PAGERDUTY_ROUTING_KEY") or "").strip()


def spike_event(spike: Spike, key: str) -> dict:
    """PagerDuty event for a shown metric jump or monitoring gap.

    The dedup key is stable (kind + service + start), so re-scoring the same
    history does not open a second incident.
    """
    if spike.kind == "gap":
        summary = f"No metrics from {spike.service} since {spike.start:%Y-%m-%d %H:%M} UTC (monitoring gap)"
        severity = "warning"
    else:
        rise = spike.rise_at_confirm
        amount = f"{rise / 1e9:.2f} GB" if abs(rise) >= 1e6 else f"{rise:.3g}"
        verb = "has been leaking; now" if spike.kind == "leak" else "jumped"
        summary = (
            f"{spike.service}: metric {verb} {amount} above its recent median "
            f"at {spike.shown_at:%Y-%m-%d %H:%M} UTC"
        )
        severity = "error"
    return {
        "routing_key": key,
        "event_action": "trigger",
        "dedup_key": f"{spike.kind}:{spike.service}:{spike.start.isoformat()}"[:255],
        "payload": {
            "summary": summary[:1024],
            "severity": severity,
            "source": spike.service,
            "timestamp": spike.shown_at.isoformat(),
            "custom_details": {
                "kind": spike.kind,
                "start": spike.start.isoformat(),
                "confirmed_at": spike.shown_at.isoformat(),
                "rise_at_confirm": spike.rise_at_confirm,
            },
        },
    }


def _post(body: dict) -> None:
    req = Request(
        EVENTS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


def notify_escalations(
    alerts: list[Alert],
    decisions: list[Decision],
    *,
    key: str | None = None,
    spikes: list[Spike] | None = None,
) -> dict:
    """Trigger one PagerDuty event per escalated alert and per shown jump / gap.

    Call this only after the queue is scored and only when shadow mode is off.
    """
    key = key if key is not None else routing_key()
    if not key:
        return {"sent": 0, "failed": 0, "skipped": "no PAGERDUTY_ROUTING_KEY"}
    sent = failed = 0
    errors: list[str] = []
    for spike in spikes or []:
        try:
            _post(spike_event(spike, key))
            sent += 1
        except Exception as exc:  # noqa: BLE001 — keep going if one page fails
            failed += 1
            errors.append(f"{spike.kind}:{spike.service}: {exc}")
    for alert, decision in zip(alerts, decisions):
        if decision.action is not Action.ESCALATE:
            continue
        body = {
            "routing_key": key,
            "event_action": "trigger",
            "dedup_key": alert.alert_id[:255],
            "payload": {
                "summary": (alert.message or alert.alert_type)[:1024],
                "severity": _SEVERITY.get(alert.severity, "warning"),
                "source": alert.source,
                "timestamp": alert.timestamp.isoformat(),
                "custom_details": {
                    "alert_id": alert.alert_id,
                    "alert_type": alert.alert_type,
                    "score": float(decision.confidence),
                    "reason": decision.reason,
                },
            },
        }
        try:
            _post(body)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — keep scoring even if paging fails
            failed += 1
            errors.append(f"{alert.alert_id}: {exc}")
    return {"sent": sent, "failed": failed, "errors": errors[:5]}
