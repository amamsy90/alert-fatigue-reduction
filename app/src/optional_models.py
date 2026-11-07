"""Apply the optional metric model and learned ranker. Both default to off.

Neither model pages. A missing file, a bad hash, or a missing library leaves
the first-come queue unchanged. Live paging is still decided later, by the
existing gates, and only for items the queue actually showed.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .config import Config
from .metric_model import (
    load_metric_model,
    metric_model_directory,
    metric_model_enabled,
    proposals_from_scores,
)
from .queue import QueueItem, build_queue
from .ranker import learned_ranker_enabled, learned_ranker_path, load_ranker
from .ranker_audit import (
    audit_enabled,
    build_ranker_audit,
    ranker_abstain_enabled,
    ranker_disagree_enabled,
    ranker_explain_enabled,
)

log = logging.getLogger(__name__)


def finish_queue(
    candidates: list[QueueItem],
    metrics: pd.DataFrame | None,
    config: Config,
) -> tuple[list[QueueItem], dict[str, str]]:
    """The queue to show. With both flags off this is ``build_queue``."""
    status: dict[str, Any] = {"metric_model": "off", "learned_ranker": "off"}
    if not metric_model_enabled() and not learned_ranker_enabled():
        return build_queue(candidates, config), status

    items = [QueueItem(item.shown_at, item.kind, item.service, item.payload) for item in candidates]
    scores: dict | None = None
    if metric_model_enabled():
        directory = metric_model_directory()
        if directory is None:
            status["metric_model"] = "missing"
        else:
            archive, why = load_metric_model(directory)
            status["metric_model"] = why
            if archive is not None and metrics is not None and not metrics.empty:
                try:
                    scores = archive.score(metrics)
                    extra = proposals_from_scores(
                        scores, archive.threshold, items, freq=archive.freq, stride=archive.stride
                    )
                    items.extend(QueueItem(spike.shown_at, spike.kind, spike.service, spike) for spike in extra)
                    status["metric_model"] = "applied"
                except Exception:
                    log.warning("metric model did not score; the jump queue is unchanged", exc_info=True)
                    items = [QueueItem(item.shown_at, item.kind, item.service, item.payload) for item in candidates]
                    scores = None
                    status["metric_model"] = "failed"

    if learned_ranker_enabled():
        path = learned_ranker_path()
        if path is None:
            status["learned_ranker"] = "missing"
        else:
            ranker, why = load_ranker(path)
            status["learned_ranker"] = why
            if ranker is not None:
                if ranker.uses_metric_score and not scores:
                    status["learned_ranker"] = "needs-metric-model"
                else:
                    try:
                        shown = ranker.select(items, scores, config)
                        status["learned_ranker"] = "applied"
                        if audit_enabled():
                            try:
                                audit = build_ranker_audit(ranker, items, shown, scores, config)
                            except Exception:
                                log.warning("ranker audit failed; the queue is unchanged", exc_info=True)
                                audit = {
                                    "pages": False,
                                    "error": "audit-failed",
                                    "explain": "on" if ranker_explain_enabled() else "off",
                                    "abstain": "on" if ranker_abstain_enabled() else "off",
                                    "disagree": "on" if ranker_disagree_enabled() else "off",
                                    "needs_human_review": [],
                                    "review_count": 0,
                                }
                            if audit is not None:
                                status["ranker_audit"] = audit
                        return shown, status
                    except Exception:
                        log.warning("learned ranker did not score; keeping arrival order", exc_info=True)
                        status["learned_ranker"] = "failed"
    return build_queue(items, config), status
