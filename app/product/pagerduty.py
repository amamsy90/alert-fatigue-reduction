"""Send escalations to PagerDuty Events API v2. Never used while shadow mode is on."""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from src.entities import Action, Alert, Decision

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


def notify_escalations(
    alerts: list[Alert],
    decisions: list[Decision],
    *,
    key: str | None = None,
) -> dict:
    """Trigger one PagerDuty event per escalated alert. Returns sent / failed counts.

    Call this only after the queue is scored and only when shadow mode is off.
    """
    key = key if key is not None else routing_key()
    if not key:
        return {"sent": 0, "failed": 0, "skipped": "no PAGERDUTY_ROUTING_KEY"}
    sent = failed = 0
    errors: list[str] = []
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
            req = Request(
                EVENTS_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=15) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"HTTP {resp.status}")
            sent += 1
        except Exception as exc:  # noqa: BLE001 — keep scoring even if paging fails
            failed += 1
            errors.append(f"{alert.alert_id}: {exc}")
    return {"sent": sent, "failed": failed, "errors": errors[:5]}
