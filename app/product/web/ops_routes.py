"""Health, the Connectors page data, and the operator controls under /ops."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from product import ops as ops_mod
from product.monitoring import METRICS, gauges
from product.monitoring import health as service_health
from product.prometheus_adapter import probe_prometheus, run_prometheus_test
from product.web import context as ctx
from product.web.schemas import (
    OpsgenieKeyRequest,
    PromUrlRequest,
    RoutingKeyRequest,
    ShadowModeRequest,
    SlackWebhookRequest,
)
from product.web.security import _auth_settings

router = APIRouter()


@router.get("/metrics")
def metrics():
    """Prometheus text format: requests, latency, jobs, pages, ingest, and health gauges."""
    return PlainTextResponse(METRICS.render() + gauges(ctx.STORE), media_type="text/plain; version=0.0.4")


@router.get("/monitoring")
def monitoring():
    """Service, model and data health with a verdict against the targets in docs/monitoring.md."""
    return service_health(ctx.STORE)


def _connector_status() -> dict:
    """What is wired. Secrets (keys, passwords) are never returned."""
    prom_info = ops_mod.prom_url_status()  # url is already redacted
    prom = ops_mod.get_prom_url()
    stats = ctx.STORE.buffer_stats()
    shadow_info = ops_mod.shadow_status()
    key_info = ops_mod.routing_key_status()
    shadow = shadow_info["shadow_mode"]
    pd_on = key_info["configured"]
    return {
        "shadow_mode": shadow,
        "live": not shadow,
        "shadow": shadow_info,
        "login_required": _auth_settings() is not None,
        "ingest_token_required": bool(os.environ.get("INGEST_TOKEN")),
        "webhook": {
            "ready": True,
            "path": "/ingest/alertmanager",
            "also": "/ingest/alerts",
            "buffered_batches": stats["buffered_batches"],
            "last_ingest_at": stats["last_ingest_at"],
        },
        "prometheus": {
            "configured": prom_info["configured"],
            "url": prom_info.get("url"),
            "url_host": prom_info.get("url_host"),
            "source": prom_info.get("source"),
            "query_set": prom_info.get("query_set", False),
            "service_label": prom_info.get("service_label"),
            "note": prom_info.get("note"),
            "probe": probe_prometheus(prom or None) if prom else {"reachable": False, "error": "PROM_URL is not set"},
            "last_test": None,
        },
        "pagerduty": {
            "configured": pd_on,
            "last4": key_info["last4"],
            "key_source": key_info["source"],
            "will_page": ops_mod.can_page(),
            "last": (stats["last_job"] or {}).get("pagerduty"),
            "would_have_paged": (stats["last_job"] or {}).get("would_have_paged"),
            "note": "Only ingest jobs page when Live + key. Sample / GAIA / Evaluate never page.",
        },
        "slack": {**ops_mod.slack_status(), "will_page": ops_mod.can_page() and bool(ops_mod.get_slack_webhook())},
        "opsgenie": {**ops_mod.opsgenie_status(), "will_page": ops_mod.can_page() and bool(ops_mod.get_opsgenie_key())},
        "channels": ops_mod.configured_channels(),
        "last_job": stats["last_job"],
    }


@router.get("/health")
def health():
    """Liveness/readiness without probing Prometheus or loading connector secrets.

    A Prometheus probe on /health made orchestrator checks hang whenever Prom
    was down; keep this endpoint cheap and dependency-light.
    """
    db_ok = True
    try:
        ctx.STORE.latest_job_id()
    except Exception:  # noqa: BLE001 — degraded, not a crash
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db_ok": db_ok,
        "shadow_mode": ops_mod.get_shadow_mode(),
        "login_required": _auth_settings() is not None,
        "pagerduty_configured": ops_mod.routing_key_status()["configured"],
        "channels": ops_mod.configured_channels(),
    }


@router.get("/connectors")
def connectors():
    return _connector_status()


@router.post("/ops/shadow")
def ops_set_shadow(body: ShadowModeRequest):
    """Switch Shadow vs Live. Live ON requires confirm='LIVE' and a login password."""
    if not body.shadow_mode and _auth_settings() is None:
        raise HTTPException(400, "Set APP_PASSWORD before turning Live on: anyone who can open the app could page people.")
    try:
        return ops_mod.set_shadow_mode(shadow=body.shadow_mode, confirm=body.confirm)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/ops/pagerduty/key")
def ops_set_routing_key(body: RoutingKeyRequest):
    """Save a routing key (never returned in full on GET)."""
    try:
        return ops_mod.set_routing_key(body.routing_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/ops/pagerduty/key")
def ops_clear_routing_key():
    """Clear the saved routing key (env key still applies if set)."""
    return ops_mod.clear_routing_key()


@router.post("/ops/pagerduty/test")
def ops_test_page():
    """Send one test trigger. Only when Live + key are both set."""
    if ops_mod.get_shadow_mode():
        raise HTTPException(400, "Test page refused: Shadow mode is on. Turn Live on first.")
    key = ops_mod.get_routing_key()
    if not key:
        raise HTTPException(400, "Test page refused: no routing key configured.")
    from product.pagerduty import _post

    body = {
        "routing_key": key,
        "event_action": "trigger",
        "dedup_key": "oncall-queue-test",
        "payload": {
            "summary": "On-call queue test page (safe to resolve)",
            "severity": "info",
            "source": "oncall-queue",
            "custom_details": {"kind": "test"},
        },
    }
    try:
        _post(body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"PagerDuty rejected the test: {type(exc).__name__}") from exc
    return {"sent": 1, "dedup_key": "oncall-queue-test", "note": "Resolve the test incident in PagerDuty."}


@router.post("/ops/slack/webhook")
def ops_set_slack(body: SlackWebhookRequest):
    try:
        return ops_mod.set_slack_webhook(body.webhook_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/ops/slack/webhook")
def ops_clear_slack():
    return ops_mod.clear_slack_webhook()


@router.post("/ops/slack/test")
def ops_test_slack():
    """Post one test message. Only when Live and a webhook is set."""
    if ops_mod.get_shadow_mode():
        raise HTTPException(400, "Test refused: Shadow mode is on. Turn Live on first.")
    if not ops_mod.get_slack_webhook():
        raise HTTPException(400, "Test refused: no Slack webhook configured.")
    from product.notifiers import notify_slack

    item = {"kind": "test", "service": "oncall-queue", "summary": "Test message (safe to ignore)",
            "dedup_key": "oncall-queue-test", "severity": "info"}
    r = notify_slack([item], 1)
    if r.get("failed"):
        raise HTTPException(502, f"Slack rejected the test: {', '.join(r.get('errors') or [])}")
    return {"sent": 1, "note": "Check the Slack channel."}


@router.post("/ops/opsgenie/key")
def ops_set_opsgenie(body: OpsgenieKeyRequest):
    try:
        return ops_mod.set_opsgenie_key(body.api_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/ops/opsgenie/key")
def ops_clear_opsgenie():
    return ops_mod.clear_opsgenie_key()


@router.post("/ops/opsgenie/test")
def ops_test_opsgenie():
    """Create one test alert. Only when Live and a key is set."""
    if ops_mod.get_shadow_mode():
        raise HTTPException(400, "Test refused: Shadow mode is on. Turn Live on first.")
    if not ops_mod.get_opsgenie_key():
        raise HTTPException(400, "Test refused: no Opsgenie key configured.")
    from product.notifiers import notify_opsgenie

    item = {"kind": "test", "service": "oncall-queue", "summary": "On-call queue test alert (safe to close)",
            "dedup_key": "oncall-queue-test", "severity": "info"}
    r = notify_opsgenie([item])
    if r.get("failed"):
        raise HTTPException(502, f"Opsgenie rejected the test: {', '.join(r.get('errors') or [])}")
    return {"sent": 1, "alias": "oncall-queue-test", "note": "Close the test alert in Opsgenie."}


@router.post("/ops/prometheus/url")
def ops_set_prom_url(body: PromUrlRequest):
    """Save Prometheus base URL (like the PagerDuty key). PROM_QUERY stays env-only."""
    try:
        return ops_mod.set_prom_url(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/ops/prometheus/url")
def ops_clear_prom_url():
    """Clear the saved Prometheus URL (env PROM_URL still applies if set)."""
    return ops_mod.clear_prom_url()


def _prometheus_test() -> dict:
    """Probe + short query; compact result only (no series payloads)."""
    return run_prometheus_test()


@router.get("/ops/prometheus/test")
@router.post("/ops/prometheus/test")
def ops_run_prometheus_test():
    """Reach Prometheus and run a short memory query. Returns counts only."""
    # Always 200: with ok=false the UI shows how to set PROM_URL.
    return _prometheus_test()
