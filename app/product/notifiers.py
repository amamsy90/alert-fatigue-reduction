"""Slack and Opsgenie paging. Same gate as PagerDuty: Live mode, ingest jobs only,
and only the items new since the last job. Nothing here runs in shadow mode.

Both take the item list that `pagerduty.would_have_paged` builds, so the three
channels always send the same set.
"""
from __future__ import annotations

import json
from urllib.request import Request, urlopen

from .ops import get_opsgenie_key, get_slack_webhook, opsgenie_api_url

_OPSGENIE_PRIORITY = {"critical": "P1", "error": "P2", "warning": "P3", "info": "P5"}
MAX_SLACK_LINES = 20


def _post(url: str, body: dict, headers: dict | None = None, timeout: float = 15.0) -> None:
    req = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


def slack_message(items: list[dict], count: int, *, shadow: bool = False) -> dict:
    """One Slack message per job listing the items (capped), never one per item."""
    head = "On-call queue" + (" (shadow — would have paged)" if shadow else "")
    lines = [f"*{head}: {count} new item{'s' if count != 1 else ''}*"]
    for it in items[:MAX_SLACK_LINES]:
        lines.append(f"• [{it['kind']}] {it['service']}: {it['summary']}")
    if count > MAX_SLACK_LINES:
        lines.append(f"… and {count - MAX_SLACK_LINES} more")
    return {"text": "\n".join(lines)}


def notify_slack(items: list[dict], count: int, *, url: str | None = None) -> dict:
    url = url if url is not None else get_slack_webhook()
    if not url:
        return {"sent": 0, "failed": 0, "skipped": "no Slack webhook"}
    if count == 0:
        return {"sent": 0, "failed": 0, "skipped": "nothing new"}
    try:
        _post(url, slack_message(items, count))
        return {"sent": 1, "failed": 0, "items": count}
    except Exception as exc:  # noqa: BLE001 — never echo the URL (it is the secret)
        return {"sent": 0, "failed": 1, "errors": [type(exc).__name__]}


def opsgenie_alert(item: dict) -> dict:
    severity = str(item.get("severity") or ("warning" if item["kind"] == "gap" else "error"))
    return {
        "message": item["summary"][:130],
        "alias": item["dedup_key"][:512],
        "description": item["summary"][:15000],
        "source": item["service"][:100],
        "priority": _OPSGENIE_PRIORITY.get(severity, "P3"),
        "tags": ["oncall-queue", item["kind"]],
    }


def notify_opsgenie(items: list[dict], *, key: str | None = None, api_url: str | None = None) -> dict:
    key = key if key is not None else get_opsgenie_key()
    if not key:
        return {"sent": 0, "failed": 0, "skipped": "no Opsgenie key"}
    base = (api_url or opsgenie_api_url()).rstrip("/")
    sent = failed = 0
    errors: list[str] = []
    for item in items:
        try:
            _post(f"{base}/v2/alerts", opsgenie_alert(item), {"Authorization": f"GenieKey {key}"})
            sent += 1
        except Exception as exc:  # noqa: BLE001 — keep going if one alert fails
            failed += 1
            errors.append(f"{item['kind']}:{item['service']}: {type(exc).__name__}")
    return {"sent": sent, "failed": failed, "errors": errors[:5]}
