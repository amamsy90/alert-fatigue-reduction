"""Decide what a finished job pages, send it, and remember which items were seen.

Hard safety (ADR 0002): only `ingest` jobs may page, and only with Live mode,
a channel and a login password. Every job still records what Live would have
paged. Ingest jobs re-score the metric history. The first job pages only items
inside the lookback window and remembers every shown key. Later jobs page any
key that was not remembered, including a point that arrived after the previous
job's clock. Each channel remembers the keys it delivered, so a retry re-sends
only to a channel that failed. Alerts in an ingest batch are all new (the
buffer is acknowledged after each job), so they are not filtered.
"""
from __future__ import annotations

import pandas as pd

from src.entities import Action, Alert, Decision
from src.memory_spikes import Spike

from .monitoring import record_page
from .notifiers import notify_opsgenie, notify_slack
from .ops import (
    PAGEABLE_MODES,
    add_channel_delivered_keys,
    can_page,
    channel_delivered_keys,
    configured_channels,
    login_configured,
    page_cutoff,
    seen_spike_keys,
    set_channel_watermark,
    set_page_watermark,
    set_seen_spike_keys,
)
from .pagerduty import notify_escalations, page_items, spike_key, would_have_paged


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


def _fresh_spikes(shown: list[Spike], run_at: pd.Timestamp, seen: set[str] | None) -> list[Spike]:
    """Items this job should page.

    Before any key has been stored, only the lookback window counts, so a first
    run does not page two weeks of history. After that, a key that was never
    stored is new even when its timestamp is behind the job clock.
    """
    if seen is None:
        cutoff = page_cutoff(run_at)
        return [s for s in shown if cutoff < s.shown_at <= run_at]
    return [s for s in shown if spike_key(s) not in seen]


def _remember(shown: list[Spike], fresh: list[Spike], seen: set[str] | None, success: bool) -> None:
    """Store keys we have finished with.

    A failed send leaves the fresh keys unstored so the next job retries them.
    Keys outside that set (old history on the first run) are stored either way,
    so a retry does not page the whole backlog.
    """
    base = set() if seen is None else set(seen)
    shown_keys = {spike_key(s) for s in shown}
    fresh_keys = {spike_key(s) for s in fresh}
    if success:
        set_seen_spike_keys(base | shown_keys)
    else:
        set_seen_spike_keys(base | (shown_keys - fresh_keys))


def _send(alerts: list[Alert], decisions: list[Decision], spikes: list[Spike], run_at: pd.Timestamp) -> dict:
    channels = configured_channels()
    per: dict[str, dict] = {}
    escalate = any(d.action is Action.ESCALATE for d in decisions)

    def _pending(channel: str) -> list[Spike]:
        done = channel_delivered_keys(channel)
        return [s for s in spikes if spike_key(s) not in done]

    if "pagerduty" in channels:
        pending = _pending("pagerduty")
        if pending or escalate:
            per["pagerduty"] = notify_escalations(alerts, decisions, spikes=pending)
    if "slack" in channels:
        pending = _pending("slack")
        if pending or escalate:
            items = page_items(alerts, decisions, spikes=pending)
            per["slack"] = notify_slack(items, len(items))
    if "opsgenie" in channels:
        pending = _pending("opsgenie")
        if pending or escalate:
            per["opsgenie"] = notify_opsgenie(page_items(alerts, decisions, spikes=pending))
    for channel, outcome in per.items():
        record_page(channel, int(outcome.get("sent", 0)), int(outcome.get("failed", 0)))
        if not outcome.get("failed"):
            add_channel_delivered_keys(channel, {spike_key(s) for s in _pending(channel)})
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
    shown = list(shown_spikes or [])
    fresh = shown
    seen = None
    if mode in PAGEABLE_MODES:
        seen = seen_spike_keys()
        fresh = _fresh_spikes(shown, run_at, seen)
    reason = _skip_reason(mode, shadow_mode)
    page = {"sent": 0, "failed": 0, "skipped": reason} if reason else _send(alerts, decisions, fresh, run_at)
    whp = would_have_paged(alerts, decisions, spikes=fresh)
    if mode in PAGEABLE_MODES:
        if seen is None:
            whp["since"] = page_cutoff(run_at).isoformat()
        # Remember in shadow too, so turning Live on never pages the shadow period.
        # A failed send keeps the fresh keys so the next job retries them.
        success = not page.get("failed")
        _remember(shown, fresh, seen, success)
        if success:
            set_page_watermark(run_at)
    return page, whp
