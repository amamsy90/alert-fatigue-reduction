"""Decide what a finished job pages, send it, and move the page marks.

Hard safety (ADR 0002): only `ingest` jobs may page, and only with Live mode,
a channel and a login password. Every job still records what Live would have
paged. Ingest jobs re-score the metric history, so only items shown since the
last job are new; each channel keeps its own mark, so a retry re-sends only to
a channel that failed. Alerts in an ingest batch are all new (the buffer is
acknowledged after each job), so they are not filtered.
"""
from __future__ import annotations

import pandas as pd

from src.entities import Alert, Decision
from src.memory_spikes import Spike

from .notifiers import notify_opsgenie, notify_slack
from .ops import (
    PAGEABLE_MODES,
    can_page,
    channel_cutoff,
    configured_channels,
    login_configured,
    page_cutoff,
    set_channel_watermark,
    set_page_watermark,
)
from .pagerduty import notify_escalations, page_items, would_have_paged


def _skip_reason(mode: str, shadow_mode: bool) -> str | None:
    if mode not in PAGEABLE_MODES:
        return f"mode={mode} never pages"
    if shadow_mode:
        return "shadow mode"
    if not configured_channels():
        return "no channel configured"
    if not login_configured():
        return "no login password (APP_PASSWORD)"
    if not can_page():
        return "shadow mode"
    return None


def _send(alerts: list[Alert], decisions: list[Decision], spikes: list[Spike], run_at: pd.Timestamp) -> dict:
    channels = configured_channels()
    per: dict[str, dict] = {}

    def _for(channel: str) -> list[Spike]:
        # Skip metric items this channel already delivered on an earlier try.
        mark = channel_cutoff(channel, run_at)
        return [s for s in spikes if s.shown_at > mark]

    if "pagerduty" in channels:
        per["pagerduty"] = notify_escalations(alerts, decisions, spikes=_for("pagerduty"))
    if "slack" in channels:
        items = page_items(alerts, decisions, spikes=_for("slack"))
        per["slack"] = notify_slack(items, len(items))
    if "opsgenie" in channels:
        per["opsgenie"] = notify_opsgenie(page_items(alerts, decisions, spikes=_for("opsgenie")))
    for channel, outcome in per.items():
        if not outcome.get("failed"):
            set_channel_watermark(channel, run_at)
    return {
        "sent": sum(int(r.get("sent", 0)) for r in per.values()),
        "failed": sum(int(r.get("failed", 0)) for r in per.values()),
        "channels": per,
    }


def page_job(
    mode: str,
    shadow_mode: bool,
    alerts: list[Alert],
    decisions: list[Decision],
    shown_spikes: list[Spike],
    run_at: pd.Timestamp | None = None,
) -> tuple[dict, dict]:
    """Returns (paging outcome, would-have-paged summary) for the job's meta."""
    run_at = run_at if run_at is not None else pd.Timestamp.now(tz="UTC")
    spikes = list(shown_spikes or [])
    cutoff = None
    if mode in PAGEABLE_MODES:
        cutoff = page_cutoff(run_at)
        spikes = [s for s in spikes if cutoff < s.shown_at <= run_at]
    reason = _skip_reason(mode, shadow_mode)
    page = {"sent": 0, "failed": 0, "skipped": reason} if reason else _send(alerts, decisions, spikes, run_at)
    whp = would_have_paged(alerts, decisions, spikes=spikes)
    if cutoff is not None:
        whp["since"] = cutoff.isoformat()
        # Advance in shadow too, so turning Live on never pages the shadow period.
        # Keep the old mark if a send failed, so the next job retries it.
        if not page.get("failed"):
            set_page_watermark(run_at)
    return page, whp
