"""Optional queue experiments. Each one stays off unless its flag is on.

RANKER_COMPANY_CALIBRATION, HYBRID_QUEUE and RANKER_TOPOLOGY_FEATURES accept
``1``, ``true``, ``yes`` or ``on``. Unset means off. None of these flags page,
and none of them change ``LEARNED_RANKER``, ``METRIC_MODEL`` or ``LLM_EXPLAIN``.

Company calibration may use a held-out company's labels only before the
boundary passed in. Topology columns are added only on the topology frame;
``candidate_frame`` and ``FEATURES`` stay the eleven training columns.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from .config import Config, day_of
from .entities import Alert, Fault
from .eval_stats import bootstrap_mean_ci
from .queue import QueueItem, build_queue
from .ranker import (
    FEATURES,
    LearnedRanker,
    candidate_frame,
    caught_fault_ids,
    false_item_count,
    fit_ranker,
    item_label,
    select_by_score,
    tune_score_threshold,
)
from .scoring import faults_by_service, spike_hits

_ON = {"1", "true", "yes", "on"}
CALIBRATION_DAYS = (1, 2, 3)
CALIBRATION_WEIGHT = 4.0
HYBRID_FIXED_M = (10, 20)
NEIGHBOUR_MINUTES = 15
BOOTSTRAP_DRAWS = 4000
BOOTSTRAP_SEED = 42
TOPOLOGY_FEATURES = ("neighbour_alerts_15m", "same_service_jump", "fanout_degree")

# Public demo call graphs, restricted to the service names in homelab/catalog.py.
# The upstream manifests are not vendored here. Edges are not estimated from
# faults. Fan-out is the out-degree; neighbour counts use either direction.
# ``frontend`` exists in two demos, so the graph is keyed by app.
DEMO_OUT: dict[str, dict[str, tuple[str, ...]]] = {
    "online-boutique": {
        "frontend": (
            "checkoutservice",
            "cartservice",
            "productcatalogservice",
            "currencyservice",
            "recommendationservice",
            "adservice",
            "shippingservice",
        ),
        "checkoutservice": (
            "paymentservice",
            "emailservice",
            "currencyservice",
            "cartservice",
            "shippingservice",
            "productcatalogservice",
        ),
        "recommendationservice": ("productcatalogservice",),
        "cartservice": ("redis-cart",),
    },
    "otel-demo": {
        "frontend": ("checkout", "cart", "product-catalog", "currency", "recommendation", "ad", "shipping"),
        "checkout": ("payment", "email", "currency", "cart", "shipping", "product-catalog", "quote"),
        "recommendation": ("product-catalog",),
    },
    "sock-shop": {
        "front-end": ("catalogue", "carts", "orders", "user"),
        "orders": ("payment", "shipping", "carts", "user"),
        "shipping": ("queue-master",),
    },
}

# Homelab Alertmanager route, the same constants as the day-12 feedback script.
AM_GROUP_WAIT = pd.Timedelta(seconds=10)
AM_GROUP_INTERVAL = pd.Timedelta(minutes=2)
AM_NULL_ROUTE = frozenset({"Watchdog", "InfoInhibitor"})


def flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ON


def require_xgboost(backend: str) -> None:
    """Refuse a requested XGBoost fit when the library is not installed.

    ``fit_ranker`` would otherwise keep going with the pointwise model.
    """
    from .ranker import xgboost_importable

    if backend == "xgboost" and not xgboost_importable():
        raise SystemExit(
            "backend xgboost was requested but xgboost cannot be imported. "
            "Nothing was scored. Use the gpu dependency group "
            "(uv run --directory app --group gpu) or the existing GPU virtualenv."
        )


def company_calibration_enabled() -> bool:
    return flag_enabled("RANKER_COMPANY_CALIBRATION")


def hybrid_queue_enabled() -> bool:
    return flag_enabled("HYBRID_QUEUE")


def topology_features_enabled() -> bool:
    return flag_enabled("RANKER_TOPOLOGY_FEATURES")


def _use(name: str, override: bool | None) -> bool:
    if override is not None:
        return bool(override)
    return flag_enabled(name)


def as_utc(stamp: pd.Timestamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def slice_before(
    items: list[QueueItem], faults: list[Fault], boundary: pd.Timestamp
) -> tuple[list[QueueItem], list[Fault]]:
    """Items shown and faults started strictly before ``boundary``."""
    end = as_utc(boundary)
    kept_items = [item for item in items if as_utc(item.shown_at) < end]
    kept_faults = [fault for fault in faults if as_utc(fault.start) < end]
    return kept_items, kept_faults


def _scale(features: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = features.mean(axis=0)
    scale = features.std(axis=0)
    scale = np.where(scale >= 1e-6, scale, 1.0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    scaled = (features - mean) / scale
    return np.nan_to_num(scaled, nan=0.0, posinf=0.0, neginf=0.0), mean, scale


def _pointwise(frame: pd.DataFrame, labels: np.ndarray, weights: np.ndarray, seed: int) -> LearnedRanker:
    raw = frame[list(FEATURES)].to_numpy(dtype=np.float64)
    scaled, mean, scale = _scale(raw)
    model = LogisticRegression(max_iter=500, random_state=seed)
    model.fit(scaled, labels, sample_weight=weights)
    return LearnedRanker(
        backend="pointwise",
        device="cpu",
        threshold=0.0,
        mean=mean,
        scale=scale,
        uses_metric_score=bool(np.any(frame["metric_score"].to_numpy() != 0)),
        coef=np.asarray(model.coef_[0], dtype=np.float64),
        intercept=float(model.intercept_[0]),
    )


def fine_tune_ranker(
    source_items: list[QueueItem],
    source_faults: list[Fault],
    calib_items: list[QueueItem],
    calib_faults: list[Fault],
    config: Config,
    boundary: pd.Timestamp,
    *,
    seed: int = BOOTSTRAP_SEED,
    enabled: bool | None = None,
) -> tuple[LearnedRanker | None, str]:
    """Refit a pointwise ranker with extra weight on labels before ``boundary``.

    Rows at or after ``boundary`` are dropped before the fit. The threshold is
    chosen on the calibration rows only. Sample weighting is the reweight step;
    the saved ranker has no live booster to continue.
    """
    if not _use("RANKER_COMPANY_CALIBRATION", enabled):
        return None, "off"
    src_items, src_faults = slice_before(source_items, source_faults, boundary)
    cal_items, cal_faults = slice_before(calib_items, calib_faults, boundary)
    if not cal_items:
        return None, "no-calibration-items"
    src_y = np.asarray([item_label(item, src_faults) for item in src_items], dtype=int)
    cal_y = np.asarray([item_label(item, cal_faults) for item in cal_items], dtype=int)
    src_w = np.where(src_y == 1, config.miss_cost, config.false_alarm_cost).astype(float)
    cal_w = np.where(cal_y == 1, config.miss_cost, config.false_alarm_cost).astype(float) * CALIBRATION_WEIGHT
    frames = []
    labels = []
    weights = []
    if src_items:
        frames.append(candidate_frame(src_items, None, config))
        labels.append(src_y)
        weights.append(src_w)
    frames.append(candidate_frame(cal_items, None, config))
    labels.append(cal_y)
    weights.append(cal_w)
    frame = pd.concat(frames, ignore_index=True)
    y = np.concatenate(labels)
    w = np.concatenate(weights)
    if len(y) < 2 or len(set(y.tolist())) < 2:
        return None, "one-class"
    ranker = _pointwise(frame, y, w, seed)
    scores = ranker.score_frame(candidate_frame(cal_items, None, config))
    ranker.threshold = tune_score_threshold(cal_items, scores, cal_faults, config)
    return ranker, "trained"


def recalibrate_threshold(
    source: LearnedRanker | None,
    calib_items: list[QueueItem],
    calib_faults: list[Fault],
    config: Config,
    boundary: pd.Timestamp,
    *,
    enabled: bool | None = None,
) -> tuple[LearnedRanker | None, str]:
    """Copy ``source`` and retune its threshold on labels before ``boundary``."""
    if not _use("RANKER_COMPANY_CALIBRATION", enabled):
        return source, "off"
    if source is None:
        return None, "no-source"
    items, faults = slice_before(calib_items, calib_faults, boundary)
    adapted = replace(source)
    if not items:
        adapted.threshold = 1.0
        return adapted, "no-calibration-items"
    adapted.retune(items, faults, config, None)
    return adapted, "retuned"


def _key(item: QueueItem) -> tuple[str, str, str]:
    return (str(as_utc(item.shown_at)), item.kind, item.service)


def hybrid_from_queues(
    ranked: list[QueueItem],
    proposed: list[QueueItem],
    config: Config,
    m: int,
    *,
    enabled: bool | None = None,
) -> list[QueueItem]:
    """Ranker items fill the first M slots of each day. Proposed fills the rest.

    With the flag off this returns the proposed queue unchanged.
    """
    if not _use("HYBRID_QUEUE", enabled):
        return list(proposed)
    cap = config.budget_k
    m_applied = max(0, min(int(m), cap))
    ranked_days: dict[pd.Timestamp, list[QueueItem]] = {}
    proposed_days: dict[pd.Timestamp, list[QueueItem]] = {}
    for item in ranked:
        ranked_days.setdefault(day_of(item.shown_at, config), []).append(item)
    for item in proposed:
        proposed_days.setdefault(day_of(item.shown_at, config), []).append(item)
    shown: list[QueueItem] = []
    for day in sorted(set(ranked_days) | set(proposed_days)):
        chosen: list[QueueItem] = []
        seen: set[tuple[str, str, str]] = set()
        takers = 0
        for item in ranked_days.get(day, []):
            if not item.attached and takers >= m_applied:
                continue
            if item.attached and not any(
                (not other.attached) and other.service == item.service for other in chosen
            ):
                continue
            chosen.append(item)
            seen.add(_key(item))
            takers += not item.attached
        for item in proposed_days.get(day, []):
            if _key(item) in seen:
                continue
            if item.attached:
                if any((not other.attached) and other.service == item.service for other in chosen):
                    chosen.append(item)
                    seen.add(_key(item))
                continue
            if takers >= cap:
                continue
            chosen.append(item)
            seen.add(_key(item))
            takers += 1
        shown.extend(chosen)
    return shown


def hybrid_queue(
    items: list[QueueItem],
    ranker: LearnedRanker,
    config: Config,
    m: int,
    *,
    enabled: bool | None = None,
) -> list[QueueItem]:
    """Build the hybrid from a learned ranker. Flag off returns ``build_queue``."""
    if not _use("HYBRID_QUEUE", enabled):
        return build_queue(items, config)
    m_applied = max(0, min(int(m), config.budget_k))
    ranked = ranker.select(items, None, replace(config, budget_k=m_applied))
    proposed = build_queue(items, config)
    return hybrid_from_queues(ranked, proposed, config, m_applied, enabled=True)


def queue_cost(shown: list[QueueItem], faults: list[Fault], config: Config) -> float:
    missed = len(faults) - len(caught_fault_ids(shown, faults))
    return config.miss_cost * missed + config.false_alarm_cost * false_item_count(shown, faults)


def choose_hybrid_m(
    items: list[QueueItem],
    faults: list[Fault],
    ranker: LearnedRanker,
    config: Config,
    candidates: list[int],
    *,
    enabled: bool | None = None,
) -> int | None:
    """Lowest training cost. A tie keeps the smaller M. Flag off returns None."""
    if not _use("HYBRID_QUEUE", enabled) or not candidates:
        return None
    best_m: int | None = None
    best_cost: float | None = None
    for m in candidates:
        shown = hybrid_queue(items, ranker, config, m, enabled=True)
        cost = queue_cost(shown, faults, config)
        if best_cost is None or cost < best_cost or (cost == best_cost and best_m is not None and m < best_m):
            best_m = int(m)
            best_cost = cost
    return best_m


def _split_service(service: str) -> tuple[str, str]:
    if "/" in service:
        company, name = service.split("/", 1)
        return company, name
    return "", service


def _app_graph(service: str, apps: dict[str, str] | None) -> dict[str, tuple[str, ...]]:
    """The one demo graph for this service. Ambiguous or unknown names get none."""
    company, _name = _split_service(service)
    if apps and company in apps and apps[company] in DEMO_OUT:
        return DEMO_OUT[apps[company]]
    return {}


def fanout_degree(service: str, apps: dict[str, str] | None = None) -> int:
    _company, name = _split_service(service)
    graph = _app_graph(service, apps)
    return len(graph.get(name, ()))


def neighbour_services(service: str, apps: dict[str, str] | None = None) -> set[str]:
    company, name = _split_service(service)
    graph = _app_graph(service, apps)
    short = set(graph.get(name, ()))
    short.update(src for src, dests in graph.items() if name in dests)
    if company:
        return {f"{company}/{item}" for item in short}
    return short


def topology_frame(
    items: list[QueueItem],
    metric_scores: dict | None,
    config: Config,
    *,
    apps: dict[str, str] | None = None,
    enabled: bool | None = None,
) -> pd.DataFrame:
    """Standard ranker columns, plus neighbour features only when the flag is on."""
    frame = candidate_frame(items, metric_scores, config)
    if not _use("RANKER_TOPOLOGY_FEATURES", enabled):
        return frame
    window = pd.Timedelta(minutes=NEIGHBOUR_MINUTES)
    alerts = [item for item in items if item.kind == "alert"]
    jumps = [item for item in items if item.kind in {"jump", "leak", "rule"}]
    neighbour_counts = []
    jump_flags = []
    degrees = []
    for item in items:
        stamp = as_utc(item.shown_at)
        neighbours = neighbour_services(item.service, apps)
        count = 0
        for alert in alerts:
            if alert.service not in neighbours:
                continue
            when = as_utc(alert.shown_at)
            if stamp - window < when <= stamp:
                count += 1
        active = 0
        for jump in jumps:
            if jump is item or jump.service != item.service:
                continue
            if jump.spike.open_at(stamp):
                active = 1
                break
        neighbour_counts.append(count)
        jump_flags.append(active)
        degrees.append(fanout_degree(item.service, apps))
    extra = frame.copy()
    extra["neighbour_alerts_15m"] = neighbour_counts
    extra["same_service_jump"] = jump_flags
    extra["fanout_degree"] = degrees
    return extra


@dataclass
class TopologyRanker:
    threshold: float
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray | None
    intercept: float
    feature_names: tuple[str, ...]
    apps: dict[str, str] | None = None
    backend: str = "pointwise"
    trees: list[np.ndarray] | None = None
    base_score: float = 0.0
    device: str = "cpu"

    def score_items(
        self,
        items: list[QueueItem],
        metric_scores: dict | None,
        config: Config,
    ) -> np.ndarray:
        from .ranker import tree_margins

        frame = topology_frame(items, metric_scores, config, apps=self.apps, enabled=True)
        raw = frame[list(self.feature_names)].to_numpy(dtype=np.float64)
        scaled = (raw - self.mean) / self.scale
        scaled = np.nan_to_num(scaled, nan=0.0, posinf=0.0, neginf=0.0)
        if self.backend == "xgboost":
            if not self.trees:
                raise RuntimeError("xgboost topology ranker has no trees")
            return tree_margins(self.trees, self.base_score, scaled)
        if self.coef is None:
            raise RuntimeError("pointwise topology ranker has no coefficients")
        logits = np.clip(scaled @ self.coef + self.intercept, -30, 30)
        return 1.0 / (1.0 + np.exp(-logits))

    def select(
        self,
        items: list[QueueItem],
        metric_scores: dict | None,
        config: Config,
    ) -> list[QueueItem]:
        if not items:
            return []
        return select_by_score(items, self.score_items(items, metric_scores, config), config, self.threshold)


def fit_topology_ranker(
    items: list[QueueItem],
    faults: list[Fault],
    config: Config,
    train_end: pd.Timestamp,
    *,
    seed: int = BOOTSTRAP_SEED,
    apps: dict[str, str] | None = None,
    backend: str = "pointwise",
    device: str = "cpu",
    enabled: bool | None = None,
) -> tuple[TopologyRanker | None, str]:
    """Pointwise ranker on the standard columns plus the topology columns.

    Training rows are strictly before ``train_end``. Flag off does not fit.
    """
    if not _use("RANKER_TOPOLOGY_FEATURES", enabled):
        return None, "off"
    kept_items, kept_faults = slice_before(items, faults, train_end)
    if len(kept_items) < 2:
        return None, "one-class"
    frame = topology_frame(kept_items, None, config, apps=apps, enabled=True)
    labels = np.asarray([item_label(item, kept_faults) for item in kept_items], dtype=int)
    if len(set(labels.tolist())) < 2:
        return None, "one-class"
    names = tuple(FEATURES) + TOPOLOGY_FEATURES
    raw = frame[list(names)].to_numpy(dtype=np.float64)
    scaled, mean, scale = _scale(raw)
    weights = np.where(labels == 1, config.miss_cost, config.false_alarm_cost)
    used = backend
    coef = None
    intercept = 0.0
    trees = None
    base = 0.0
    resolved = "cpu"
    if backend == "xgboost":
        try:
            from .ranker_xgb import fit_xgboost_ranker

            groups = np.asarray([day_of(item.shown_at, config).value for item in kept_items], dtype=np.int64)
            trees, base, resolved = fit_xgboost_ranker(scaled, labels, groups, seed=seed, device=device)
        except Exception:
            used = "pointwise"
    if used == "pointwise":
        model = LogisticRegression(max_iter=500, random_state=seed)
        model.fit(scaled, labels, sample_weight=weights)
        coef = np.asarray(model.coef_[0], dtype=np.float64)
        intercept = float(model.intercept_[0])
        resolved = "cpu"
    ranker = TopologyRanker(
        threshold=0.0,
        mean=mean,
        scale=scale,
        coef=coef,
        intercept=intercept,
        feature_names=names,
        apps=apps,
        backend=used,
        trees=trees,
        base_score=float(base),
        device=resolved,
    )
    scores = ranker.score_items(kept_items, None, config)
    ranker.threshold = tune_score_threshold(kept_items, scores, kept_faults, config)
    return ranker, "trained" if used == backend else "trained-pointwise-fallback"


def compare_caught(
    candidate: set[str],
    baseline: set[str],
    faults: list[Fault],
    *,
    n_draw: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Paired bootstrap of per-fault catch difference. KEEP only if low > 0."""
    if not faults:
        return {"status": "not-scored", "reason": "no faults", "decision": None, "faults": 0}
    delta = [float(fault.fault_id in candidate) - float(fault.fault_id in baseline) for fault in faults]
    interval = bootstrap_mean_ci(delta, n_draw=n_draw, seed=seed)
    return {
        "status": "scored",
        "faults": len(faults),
        "candidate_caught": len(candidate & {fault.fault_id for fault in faults}),
        "baseline_caught": len(baseline & {fault.fault_id for fault in faults}),
        "mean": interval.mean,
        "low": interval.low,
        "high": interval.high,
        "n": interval.n,
        "decision": "KEEP" if interval.low > 0 else "DROP",
    }


def first_catch_minutes(shown: list[QueueItem], faults: list[Fault]) -> dict[str, float]:
    """Minutes from fault start to the earliest shown item that catches it.

    Gaps do not count. A fault with no catching item is absent.
    """
    known = {fault.fault_id: fault for fault in faults}
    by_service = faults_by_service(faults)
    first: dict[str, pd.Timestamp] = {}
    for item in shown:
        if item.kind == "gap":
            continue
        if item.kind == "alert":
            ids = [item.alert.fault_id] if item.alert.fault_id in known else []
        else:
            ids = [fault.fault_id for fault in spike_hits(item.spike, by_service) if fault.fault_id in known]
        for fault_id in ids:
            stamp = as_utc(item.shown_at)
            if fault_id not in first or stamp < first[fault_id]:
                first[fault_id] = stamp
    return {
        fault_id: (stamp - as_utc(known[fault_id].start)).total_seconds() / 60.0
        for fault_id, stamp in first.items()
    }


def _median_ci(values: list[float], n_draw: int, seed: int) -> tuple[float, float, float] | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    draws = np.median(arr[rng.integers(0, len(arr), size=(n_draw, len(arr)))], axis=1)
    return float(np.median(arr)), float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def delay_report(
    candidate: dict[str, float],
    proposed: dict[str, float],
    *,
    n_draw: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Median and p90 delay, and a bootstrap CI on the median gap versus proposed.

    The paired gap uses faults both queues caught. It is not a KEEP rule.
    """
    if not candidate:
        return {
            "n_caught": 0,
            "median_min": None,
            "p90_min": None,
            "n_both": 0,
            "median_diff": None,
            "low": None,
            "high": None,
        }
    arr = np.asarray(list(candidate.values()), dtype=float)
    both = [candidate[fault_id] - proposed[fault_id] for fault_id in candidate if fault_id in proposed]
    paired = _median_ci(both, n_draw, seed)
    return {
        "n_caught": len(candidate),
        "median_min": float(np.median(arr)),
        "p90_min": float(np.quantile(arr, 0.9)),
        "n_both": len(both),
        "median_diff": None if paired is None else paired[0],
        "low": None if paired is None else paired[1],
        "high": None if paired is None else paired[2],
    }


def alertmanager_notifications(alerts: list[Alert], config: Config) -> list[dict]:
    """One notification per group flush. Watchdog and InfoInhibitor go nowhere."""
    groups: dict[tuple[str, str], list[Alert]] = {}
    for alert in sorted(alerts, key=lambda item: as_utc(item.timestamp)):
        if alert.alert_type in AM_NULL_ROUTE:
            continue
        groups.setdefault((alert.alert_type, alert.source), []).append(alert)
    notes: list[dict] = []
    for key, grouped in groups.items():
        current: dict | None = None
        for alert in grouped:
            if current is not None and as_utc(alert.timestamp) <= current["sent_at"]:
                current["alerts"].append(alert)
                continue
            if current is None or as_utc(alert.timestamp) - current["sent_at"] >= AM_GROUP_INTERVAL:
                sent = as_utc(alert.timestamp) + AM_GROUP_WAIT
            else:
                sent = current["sent_at"] + AM_GROUP_INTERVAL
            current = {"group": key, "sent_at": sent, "alerts": [alert]}
            notes.append(current)
    for note in notes:
        note["sev"] = max(config.severity_rank.get(alert.severity, 0) for alert in note["alerts"])
        note["fault_ids"] = {alert.fault_id for alert in note["alerts"] if alert.fault_id}
    return notes


def alertmanager_queue(notes: list[dict], config: Config, order: str) -> list[dict]:
    """Daily budget. ``severity`` sorts high severity first; ``first`` is time order."""
    by_day: dict[pd.Timestamp, list[dict]] = {}
    for note in notes:
        by_day.setdefault(day_of(note["sent_at"], config), []).append(note)
    shown: list[dict] = []
    for day_notes in by_day.values():
        if order == "severity":
            ordered = sorted(day_notes, key=lambda note: (-note["sev"], note["sent_at"]))
        else:
            ordered = sorted(day_notes, key=lambda note: note["sent_at"])
        shown.extend(ordered[: config.budget_k])
    return shown


def note_catch_minutes(shown: list[dict], faults: list[Fault]) -> dict[str, float]:
    known = {fault.fault_id: fault for fault in faults}
    first: dict[str, pd.Timestamp] = {}
    for note in shown:
        for fault_id in note["fault_ids"]:
            if fault_id not in known:
                continue
            stamp = as_utc(note["sent_at"])
            if fault_id not in first or stamp < first[fault_id]:
                first[fault_id] = stamp
    return {
        fault_id: (stamp - as_utc(known[fault_id].start)).total_seconds() / 60.0
        for fault_id, stamp in first.items()
    }


def caught_from_notes(shown: list[dict], faults: list[Fault]) -> set[str]:
    known = {fault.fault_id for fault in faults}
    caught: set[str] = set()
    for note in shown:
        caught.update(fault_id for fault_id in note["fault_ids"] if fault_id in known)
    return caught


# Re-exported so a caller can fit the unadapted ranker without a second import path.
__all__ = [
    "BOOTSTRAP_DRAWS",
    "BOOTSTRAP_SEED",
    "CALIBRATION_DAYS",
    "CALIBRATION_WEIGHT",
    "FEATURES",
    "HYBRID_FIXED_M",
    "TOPOLOGY_FEATURES",
    "TopologyRanker",
    "alertmanager_notifications",
    "alertmanager_queue",
    "choose_hybrid_m",
    "company_calibration_enabled",
    "compare_caught",
    "delay_report",
    "fanout_degree",
    "fine_tune_ranker",
    "first_catch_minutes",
    "fit_ranker",
    "fit_topology_ranker",
    "hybrid_from_queues",
    "hybrid_queue",
    "hybrid_queue_enabled",
    "note_catch_minutes",
    "recalibrate_threshold",
    "slice_before",
    "topology_features_enabled",
    "topology_frame",
]
