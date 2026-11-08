"""Optional ranker audit: explanations, confidence review, disagreement flags.

All three flags default off. With them off this module is not called, and the
queue ``finish_queue`` returns is the same list it returned before.

``RANKER_EXPLAIN`` adds per-item reasons for the learned ranker. The reason is
exact interventional SHAP against the training mean (the zero vector in scaled
space). Contributions sum to the score the queue uses. The ``shap`` package is
not a dependency, and XGBoost ``pred_contribs`` is not available: the saved
ranker stores numeric trees, not a booster.

``RANKER_ABSTAIN`` lists candidates the ranker did not show and is not
confident are noise. They go to ``needs_human_review``. They do not take a K
slot and they are not passed to the pager.

``RANKER_DISAGREE`` flags a strong disagreement between the learned ranker and
a second opinion. The second opinion is the contrastive log encoder when
``LOG_ENCODER=contrastive`` and that file loads; otherwise, and for items that
have no log template, it is the first-come queue. A flagged item that is
already out of the queue is listed for review. A flagged item that is in the
queue stays in the queue and stays pageable: removing it would change the page
set. The flag does not add a page.

The coverage curve in ``run_ranker_audit.py`` is a hypothetical policy. It is
not applied to the live queue.
"""
from __future__ import annotations

import math
import os
from typing import Any

import numpy as np
import pandas as pd

from .config import Config
from .entities import Fault
from .features import normalize_message
from .queue import QueueItem, build_queue
from .ranker import FEATURES, LearnedRanker, candidate_frame, item_label

_ON = frozenset({"1", "true", "yes", "on"})
TOP_REASONS = 3
STORED_ITEMS = 40
EXPLAIN_MAX_DEFAULT = 256
# A reported operating point is "reached" only this close to the target coverage.
COVERAGE_TOLERANCE = 0.02


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ON


def ranker_explain_enabled() -> bool:
    return _enabled("RANKER_EXPLAIN")


def ranker_abstain_enabled() -> bool:
    return _enabled("RANKER_ABSTAIN")


def ranker_disagree_enabled() -> bool:
    return _enabled("RANKER_DISAGREE")


def audit_enabled() -> bool:
    return ranker_explain_enabled() or ranker_abstain_enabled() or ranker_disagree_enabled()


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _explain_max() -> int:
    raw = os.environ.get("RANKER_EXPLAIN_MAX", "").strip()
    if not raw:
        return EXPLAIN_MAX_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return EXPLAIN_MAX_DEFAULT
    return max(1, min(value, 4096))


def interventional_shap(predict: Any, rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Exact interventional SHAP of ``predict`` against the zero vector.

    ``predict`` maps an ``(n, m)`` matrix to ``n`` scores. For feature ``j`` and
    subset ``S`` of the other features, the weight is
    ``|S|! (m - |S| - 1)! / m!`` and the difference is the score with ``j`` set
    to its row value minus the score with ``j`` left at 0. The other features
    in ``S`` keep their row values. Features outside ``S`` stay 0.

    Returns ``(phi, baseline)`` with ``phi.sum(axis=1) + baseline == predict(rows)``.
    A feature whose row value is already 0 has contribution 0.
    """
    x = np.ascontiguousarray(rows, dtype=np.float64)
    if x.ndim != 2:
        raise ValueError("shap expects a 2-d matrix")
    n, m = x.shape
    if n == 0:
        return np.zeros((0, m), dtype=np.float64), np.zeros(0, dtype=np.float64)
    if m > 16:
        raise ValueError(f"interventional shap supports at most 16 features, got {m}")
    if m == 0:
        base = np.asarray(predict(x), dtype=np.float64).reshape(-1)
        return np.zeros((n, 0), dtype=np.float64), base

    n_masks = 1 << m
    preds = np.empty((n_masks, n), dtype=np.float64)
    for mask in range(n_masks):
        hybrid = np.zeros_like(x)
        for j in range(m):
            if (mask >> j) & 1:
                hybrid[:, j] = x[:, j]
        pred = np.asarray(predict(hybrid), dtype=np.float64).reshape(-1)
        if pred.shape != (n,):
            raise ValueError("predict must return one score per row")
        preds[mask] = pred

    fact = [math.factorial(i) for i in range(m + 1)]
    denom = float(fact[m])
    phi = np.zeros((n, m), dtype=np.float64)
    for j in range(m):
        bit = 1 << j
        for mask in range(n_masks):
            if mask & bit:
                continue
            size = mask.bit_count()
            weight = fact[size] * fact[m - size - 1] / denom
            phi[:, j] += weight * (preds[mask | bit] - preds[mask])
    return phi, preds[0]


def top_reasons(phi_row: np.ndarray, values: np.ndarray, names: tuple[str, ...] | list[str], k: int = TOP_REASONS) -> list[dict]:
    """Largest absolute contributions. ``value`` is the unscaled feature."""
    order = np.argsort(-np.abs(np.asarray(phi_row, dtype=float)))
    reasons: list[dict[str, Any]] = []
    for index in order:
        contribution = float(phi_row[index])
        if contribution == 0.0 and reasons:
            break
        reasons.append(
            {
                "feature": str(names[int(index)]),
                "value": round(float(values[index]), 6),
                "contribution": round(contribution, 6),
                "direction": "up" if contribution > 0 else "down" if contribution < 0 else "flat",
            }
        )
        if len(reasons) == k:
            break
    return reasons


def margin_gaps(scores: np.ndarray, threshold: float) -> np.ndarray:
    return np.abs(np.asarray(scores, dtype=float) - float(threshold))


def default_abstain_delta(scores: np.ndarray, threshold: float) -> float:
    """Items closer to the gate than this are not confident. 25th percentile."""
    gaps = margin_gaps(scores, threshold)
    if gaps.size == 0:
        return 0.0
    return float(np.quantile(gaps, 0.25))


def default_disagree_delta(scores: np.ndarray, threshold: float) -> float:
    """A disagreement is strong when the ranker margin is at least this. 75th percentile."""
    gaps = margin_gaps(scores, threshold)
    if gaps.size == 0:
        return 0.0
    return float(np.quantile(gaps, 0.75))


def live_review_mask(
    scores: np.ndarray,
    in_queue: np.ndarray,
    attached: np.ndarray,
    threshold: float,
    delta: float,
) -> np.ndarray:
    """Dropped items the ranker is not confident are noise.

    Shown items, including alerts riding on a jump, are not in this mask.
    A confident drop is a score at or below ``threshold - delta``.
    """
    scores = np.asarray(scores, dtype=float)
    dropped = ~np.asarray(in_queue, dtype=bool) & ~np.asarray(attached, dtype=bool)
    return dropped & (scores >= float(threshold) - float(delta))


def disagreement_mask(
    scores: np.ndarray,
    in_queue: np.ndarray,
    attached: np.ndarray,
    baseline_in: np.ndarray,
    threshold: float,
    strong_delta: float,
    *,
    encoder_known: np.ndarray | None = None,
    encoder_in: np.ndarray | None = None,
) -> np.ndarray:
    """Strong disagreement with the second opinion.

    Alerts with an encoder opinion use that opinion. Every other candidate uses
    the first-come queue. ``in_queue`` and ``baseline_in`` are slot-taking
    membership, not attached riders.
    """
    attached_arr = np.asarray(attached, dtype=bool)
    ranker_in = np.asarray(in_queue, dtype=bool)
    strong = margin_gaps(scores, threshold) >= float(strong_delta)
    baseline = (ranker_in != np.asarray(baseline_in, dtype=bool)) & ~attached_arr & strong
    if encoder_known is None:
        return baseline
    known = np.asarray(encoder_known, dtype=bool)
    if encoder_in is None:
        raise ValueError("encoder_in is required when encoder_known is set")
    encoder = (ranker_in != np.asarray(encoder_in, dtype=bool)) & known & ~attached_arr & strong
    return np.asarray(np.where(known, encoder, baseline), dtype=bool)


def hypothetical_point(
    scores: np.ndarray,
    in_queue: np.ndarray,
    attached: np.ndarray,
    catches: np.ndarray,
    threshold: float,
    delta: float,
) -> dict[str, float | int | None]:
    """One point of the hypothetical abstention policy. Not applied to paging.

    Automated keep: in the ranker queue and ``score >= threshold + delta``.
    Automated drop: not in the queue and ``score <= threshold - delta``.
    Everything else among the non-attached candidates is review.
    Coverage is automated decisions over candidates. Accuracy is correct
    automated decisions over automated decisions. Precision is correct keeps
    over keeps. A keep is correct when the item's catch rule fires.
    """
    scores = np.asarray(scores, dtype=float)
    queued = np.asarray(in_queue, dtype=bool)
    attached_arr = np.asarray(attached, dtype=bool)
    hit = np.asarray(catches, dtype=bool)
    candidates = ~attached_arr
    keep = candidates & queued & (scores >= float(threshold) + float(delta))
    drop = candidates & ~queued & (scores <= float(threshold) - float(delta))
    automated = keep | drop
    review = candidates & ~automated
    n_candidates = int(candidates.sum())
    n_automated = int(automated.sum())
    n_keep = int(keep.sum())
    n_drop = int(drop.sum())
    n_keep_correct = int((keep & hit).sum())
    n_drop_correct = int((drop & ~hit).sum())
    n_correct = n_keep_correct + n_drop_correct
    return {
        "delta": float(delta),
        "candidates": n_candidates,
        "automated": n_automated,
        "review": int(review.sum()),
        "keeps": n_keep,
        "drops": n_drop,
        "coverage": None if n_candidates == 0 else n_automated / n_candidates,
        "accuracy": None if n_automated == 0 else n_correct / n_automated,
        "precision": None if n_keep == 0 else n_keep_correct / n_keep,
    }


def choose_operating_points(rows: list[dict], targets: tuple[float, ...] = (0.5, 0.7, 0.9)) -> list[dict]:
    """The swept row closest to each target coverage. ``target_reached`` is exact-enough."""
    if not rows:
        return []
    chosen = []
    for target in targets:
        best = min(rows, key=lambda row: (abs(float(row["coverage"] or 0.0) - target), float(row["delta"])))
        point = dict(best)
        coverage = float(best["coverage"] or 0.0)
        point["target_coverage"] = target
        point["target_reached"] = abs(coverage - target) <= COVERAGE_TOLERANCE
        chosen.append(point)
    return chosen


def coverage_curve(
    scores: np.ndarray,
    in_queue: np.ndarray,
    attached: np.ndarray,
    catches: np.ndarray,
    threshold: float,
) -> list[dict]:
    """Sweep ``delta`` at quantiles of ``|score - threshold|``, plus zero."""
    gaps = margin_gaps(scores, threshold)
    if gaps.size == 0:
        return []
    quantiles = np.quantile(gaps, [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
    deltas = sorted({0.0, *(float(value) for value in quantiles)})
    return [
        hypothetical_point(scores, in_queue, attached, catches, threshold, delta)
        for delta in deltas
    ]


def bootstrap_rate_difference(
    labels: np.ndarray,
    group: np.ndarray,
    *,
    n_draw: int = 4000,
    seed: int = 42,
) -> dict[str, Any]:
    """95% interval for mean(label | group) - mean(label | not group).

    Resamples candidates with replacement. A draw that empties a group is
    dropped. ``None`` when either group is empty on the original sample: there
    is no contrast to report.
    """
    y = np.asarray(labels, dtype=float)
    mask = np.asarray(group, dtype=bool)
    n_flagged = int(mask.sum())
    n_other = int((~mask).sum())
    if n_flagged == 0 or n_other == 0 or len(y) == 0:
        return {
            "status": "no-contrast",
            "reason": "one group is empty",
            "n_flagged": n_flagged,
            "n_other": n_other,
            "rate_flagged": None if n_flagged == 0 else float(y[mask].mean()),
            "rate_other": None if n_other == 0 else float(y[~mask].mean()),
            "difference": None,
        }
    rate_flagged = float(y[mask].mean())
    rate_other = float(y[~mask].mean())
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_draw, dtype=np.float64)
    n = len(y)
    kept = 0
    for _ in range(n_draw):
        idx = rng.integers(0, n, n)
        drawn_y = y[idx]
        drawn = mask[idx]
        if not drawn.any() or drawn.all():
            continue
        diffs[kept] = float(drawn_y[drawn].mean() - drawn_y[~drawn].mean())
        kept += 1
    if kept == 0:
        return {
            "status": "no-contrast",
            "reason": "every bootstrap draw emptied a group",
            "n_flagged": n_flagged,
            "n_other": n_other,
            "rate_flagged": rate_flagged,
            "rate_other": rate_other,
            "difference": None,
        }
    sample = diffs[:kept]
    return {
        "status": "scored",
        "reason": "mean catch-rate of flagged items minus mean catch-rate of the others",
        "n_flagged": n_flagged,
        "n_other": n_other,
        "rate_flagged": rate_flagged,
        "rate_other": rate_other,
        "difference": {
            "mean": float(rate_flagged - rate_other),
            "low": float(np.percentile(sample, 2.5)),
            "high": float(np.percentile(sample, 97.5)),
            "n": n,
            "draws": kept,
        },
    }


def _stamp(item: QueueItem) -> str:
    return pd.Timestamp(item.shown_at).isoformat()


def _scaled_matrix(ranker: LearnedRanker, frame: pd.DataFrame) -> np.ndarray:
    raw = frame[list(FEATURES)].to_numpy(dtype=np.float64)
    return np.nan_to_num((raw - ranker.mean) / ranker.scale, nan=0.0, posinf=0.0, neginf=0.0)


def _encoder_opinion(items: list[QueueItem], encoder: Any) -> tuple[np.ndarray, np.ndarray, str]:
    """``(known, include, partner)``. Include is a median split of alert embeddings."""
    known = np.zeros(len(items), dtype=bool)
    scores = np.full(len(items), np.nan, dtype=np.float64)
    for index, item in enumerate(items):
        if item.kind != "alert":
            continue
        embedded = np.asarray(encoder.embed(normalize_message(item.alert.message)), dtype=float)
        if embedded.size == 0:
            continue
        known[index] = True
        scores[index] = float(embedded.mean())
    include = np.zeros(len(items), dtype=bool)
    finite = scores[known]
    if finite.size:
        midpoint = float(np.median(finite))
        include[known] = scores[known] >= midpoint
    return known, include, "contrastive+baseline"


def _membership(
    items: list[QueueItem],
    shown: list[QueueItem],
    config: Config,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Slot-taking ranker membership, attached riders, slot-taking baseline membership."""
    slot_ids = {id(item.payload) for item in shown if not item.attached}
    attached_ids = {id(item.payload) for item in shown if item.attached}
    clones = [QueueItem(item.shown_at, item.kind, item.service, item.payload) for item in items]
    baseline = build_queue(clones, config)
    baseline_ids = {id(item.payload) for item in baseline if not item.attached}
    ids = [id(item.payload) for item in items]
    in_queue = np.asarray([item_id in slot_ids for item_id in ids], dtype=bool)
    attached = np.asarray([item_id in attached_ids for item_id in ids], dtype=bool)
    baseline_in = np.asarray([item_id in baseline_ids for item_id in ids], dtype=bool)
    return in_queue, attached, baseline_in


def _explain_order(in_queue: np.ndarray, review: np.ndarray, limit: int) -> np.ndarray:
    """Queue rows first, then review rows, then the rest. Stable. Capped."""
    indexes = np.arange(len(in_queue))
    rank = np.where(in_queue, 0, np.where(review, 1, 2))
    ordered = indexes[np.argsort(rank, kind="mergesort")]
    return ordered[:limit]


def _item_row(
    item: QueueItem,
    index: int,
    *,
    score: float,
    in_queue: bool,
    reasons: list[dict] | None,
    review_reason: str | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "index": index,
        "shown_at": _stamp(item),
        "kind": item.kind,
        "service": item.service,
        "score": round(float(score), 6),
        "in_queue": bool(in_queue),
        "pages": False,
    }
    if reasons is not None:
        row["reasons"] = reasons
    if review_reason is not None:
        row["review_reason"] = review_reason
    return row


def global_importance(phi: np.ndarray, names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    if phi.size == 0:
        return []
    mean_abs = np.mean(np.abs(phi), axis=0)
    order = np.argsort(-mean_abs)
    return [
        {"feature": str(names[int(index)]), "mean_abs_contribution": round(float(mean_abs[index]), 6)}
        for index in order
    ]


def _delta(given: float | None, env_name: str, default, scores: np.ndarray, threshold: float) -> float:
    """The delta passed in, else the environment setting, else the default for these scores."""
    if given is not None:
        return given
    from_env = _env_float(env_name)
    return from_env if from_env is not None else default(scores, threshold)


def _partner_opinion(items: list[QueueItem], encoder: Any | None, disagree_on: bool):
    """(known, in_queue, partner name) from the log encoder, or (None, None, "baseline") without one."""
    if not disagree_on:
        return None, None, "baseline"
    if encoder is not None:
        return _encoder_opinion(items, encoder)
    if os.environ.get("LOG_ENCODER", "").strip().lower() != "contrastive":
        return None, None, "baseline"
    from .log_encoder import load_contrastive_encoder

    loaded, error = load_contrastive_encoder()
    if loaded is None:
        return None, None, f"baseline ({error})"
    return _encoder_opinion(items, loaded)


def build_ranker_audit(
    ranker: LearnedRanker,
    items: list[QueueItem],
    shown: list[QueueItem],
    metric_scores: dict | None,
    config: Config,
    *,
    explain: bool | None = None,
    abstain: bool | None = None,
    disagree: bool | None = None,
    encoder: Any | None = None,
    abstain_delta: float | None = None,
    disagree_delta: float | None = None,
    explain_max: int | None = None,
) -> dict[str, Any] | None:
    """Shadow audit for one scored candidate list. ``None`` when every flag is off.

    Does not change ``shown``. ``pages`` is false on the audit and on every row.
    """
    explain_on = ranker_explain_enabled() if explain is None else explain
    abstain_on = ranker_abstain_enabled() if abstain is None else abstain
    disagree_on = ranker_disagree_enabled() if disagree is None else disagree
    if not (explain_on or abstain_on or disagree_on):
        return None
    if not items:
        return {
            "pages": False,
            "explain": "on" if explain_on else "off",
            "abstain": "on" if abstain_on else "off",
            "disagree": "on" if disagree_on else "off",
            "method": "interventional-shap",
            "baseline": "training-mean",
            "partner": "baseline",
            "threshold": ranker.threshold,
            "candidates": 0,
            "review_count": 0,
            "needs_human_review": [],
            "queue_reasons": [],
            "queue_disagreements": [],
        }

    frame = candidate_frame(items, metric_scores, config)
    scores = np.asarray(ranker.score_frame(frame), dtype=np.float64)
    in_queue, attached, baseline_in = _membership(items, shown, config)
    threshold = float(ranker.threshold)
    abstain_delta = _delta(abstain_delta, "RANKER_ABSTAIN_DELTA", default_abstain_delta, scores, threshold)
    disagree_delta = _delta(disagree_delta, "RANKER_DISAGREE_DELTA", default_disagree_delta, scores, threshold)
    encoder_known, encoder_in, partner = _partner_opinion(items, encoder, disagree_on)

    low_confidence = (
        live_review_mask(scores, in_queue, attached, threshold, abstain_delta)
        if abstain_on
        else np.zeros(len(items), dtype=bool)
    )
    flags = (
        disagreement_mask(
            scores,
            in_queue,
            attached,
            baseline_in,
            threshold,
            disagree_delta,
            encoder_known=encoder_known,
            encoder_in=encoder_in,
        )
        if disagree_on
        else np.zeros(len(items), dtype=bool)
    )
    # A disagreement that is already out of the queue joins the review list so it is not silent.
    dropped_flags = flags & ~in_queue & ~attached
    review = low_confidence | dropped_flags

    limit = explain_max if explain_max is not None else _explain_max()
    phi = None
    explained: set[int] = set()
    importance: list[dict] = []
    sum_gap = None
    if explain_on:
        order = _explain_order(in_queue | attached, review, limit)
        explained = set(int(index) for index in order)
        scaled = _scaled_matrix(ranker, frame)
        chosen = scaled[order]
        phi_chosen, baseline_score = interventional_shap(ranker.score_matrix, chosen)
        full_scores = scores[order]
        sum_gap = float(np.max(np.abs(phi_chosen.sum(axis=1) + baseline_score - full_scores))) if len(order) else 0.0
        phi = np.zeros((len(items), scaled.shape[1]), dtype=np.float64)
        phi[order] = phi_chosen
        importance = global_importance(phi_chosen, FEATURES)

    raw = frame[list(FEATURES)].to_numpy(dtype=np.float64)
    queue_reasons: list[dict] = []
    disagreements: list[dict] = []
    review_rows: list[dict] = []
    for index, item in enumerate(items):
        reasons = None
        if phi is not None and index in explained:
            reasons = top_reasons(phi[index], raw[index], FEATURES)
        shown_row = bool(in_queue[index] or attached[index])
        if shown_row and reasons is not None and len(queue_reasons) < STORED_ITEMS:
            row = _item_row(
                item,
                index,
                score=float(scores[index]),
                in_queue=bool(in_queue[index]),
                reasons=reasons,
                review_reason=None,
            )
            if attached[index]:
                row["attached"] = True
            if flags[index]:
                row["disagreement"] = True
            queue_reasons.append(row)
        if flags[index] and in_queue[index] and not attached[index] and len(disagreements) < STORED_ITEMS:
            disagreements.append(
                _item_row(
                    item,
                    index,
                    score=float(scores[index]),
                    in_queue=True,
                    reasons=reasons,
                    review_reason="disagreement",
                )
            )
        if review[index] and len(review_rows) < STORED_ITEMS:
            reason_names = []
            if low_confidence[index]:
                reason_names.append("low_confidence")
            if flags[index]:
                reason_names.append("disagreement")
            review_rows.append(
                _item_row(
                    item,
                    index,
                    score=float(scores[index]),
                    in_queue=False,
                    reasons=reasons,
                    review_reason="+".join(reason_names) or "review",
                )
            )

    return {
        "pages": False,
        "explain": "on" if explain_on else "off",
        "abstain": "on" if abstain_on else "off",
        "disagree": "on" if disagree_on else "off",
        "method": "interventional-shap" if explain_on else None,
        "baseline": "training-mean",
        "partner": partner,
        "score_scale": "probability" if ranker.backend == "pointwise" else "margin",
        "backend": ranker.backend,
        "threshold": threshold,
        "abstain_delta": float(abstain_delta),
        "disagree_delta": float(disagree_delta),
        "candidates": len(items),
        "explained_rows": len(explained),
        "contribution_sum_gap": None if sum_gap is None else round(sum_gap, 8),
        "global_importance": importance,
        "review_count": int(review.sum()),
        "disagreement_count": int(flags.sum()),
        "needs_human_review": review_rows,
        "queue_reasons": queue_reasons,
        "queue_disagreements": disagreements,
    }


def fault_disagreement(
    items: list[QueueItem],
    faults: list[Fault],
    disagree: np.ndarray,
    queue_caught: set[str],
) -> dict[str, int]:
    """How many missed and caught faults have a disagreeing candidate that covers them."""
    missed_with = caught_with = missed = caught = 0
    for fault in faults:
        covers = disagree & np.asarray([item_label(item, [fault]) == 1 for item in items], dtype=bool)
        has = bool(covers.any()) if len(items) else False
        if fault.fault_id in queue_caught:
            caught += 1
            caught_with += int(has)
        else:
            missed += 1
            missed_with += int(has)
    return {
        "missed": missed,
        "missed_with_disagreement": missed_with,
        "caught": caught,
        "caught_with_disagreement": caught_with,
    }


def score_candidates(
    ranker: LearnedRanker,
    items: list[QueueItem],
    shown: list[QueueItem],
    metric_scores: dict | None,
    config: Config,
    faults: list[Fault] | None = None,
    *,
    encoder: Any | None = None,
    disagree_delta: float | None = None,
) -> dict[str, Any]:
    """Arrays for the audit curve. Does not explain and does not change ``shown``."""
    frame = candidate_frame(items, metric_scores, config)
    scores = np.asarray(ranker.score_frame(frame), dtype=np.float64)
    in_queue, attached, baseline_in = _membership(items, shown, config)
    threshold = float(ranker.threshold)
    if disagree_delta is None:
        disagree_delta = default_disagree_delta(scores, threshold)
    encoder_known = encoder_in = None
    partner = "baseline"
    if encoder is not None:
        encoder_known, encoder_in, partner = _encoder_opinion(items, encoder)
    flags = disagreement_mask(
        scores,
        in_queue,
        attached,
        baseline_in,
        threshold,
        disagree_delta,
        encoder_known=encoder_known,
        encoder_in=encoder_in,
    )
    known_faults = list(faults or [])
    catches = np.asarray([item_label(item, known_faults) == 1 for item in items], dtype=bool)
    return {
        "scores": scores,
        "in_queue": in_queue,
        "attached": attached,
        "baseline_in": baseline_in,
        "catches": catches,
        "disagree": flags,
        "threshold": threshold,
        "disagree_delta": float(disagree_delta),
        "partner": partner,
        "frame": frame,
    }


def round_point(point: dict) -> dict:
    """JSON-stable operating point. Counts stay ints. Rates keep 4 decimal places."""
    out = dict(point)
    for key in ("delta", "coverage", "accuracy", "precision", "target_coverage"):
        if key in out and out[key] is not None:
            out[key] = round(float(out[key]), 4)
    return out
