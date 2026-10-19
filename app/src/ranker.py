"""Optional learned ranker for the K slots. Off unless ``LEARNED_RANKER`` is set.

Candidates are the items the rules (and, when it is on, the metric model)
already proposed. The ranker scores each one from fields known at ``shown_at``
and skips a low score so a later item can still take the slot. It does not
reorder the past, and it does not page.

Labels are the existing catch rule (a metric item overlaps a fault on its
service, or an alert carries that fault id) and are used only for items and
faults in the training window. The group is the on-call day.

Backends, in order when ``auto`` is asked: XGBoost ``rank:ndcg`` (CUDA when
the device is cuda and a device is visible, otherwise CPU), a tiny torch
pairwise net, then a scikit-learn pointwise logistic model. The saved file is
JSON. Tree boosters are stored as numeric node arrays, the same conversion as
the alert classifier. Nothing is pickled.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from .accel import NODE_DTYPE, _tree_leaves
from .config import Config, day_of
from .entities import Fault
from .queue import _KIND_ORDER, METRIC_KINDS, QueueItem
from .scoring import faults_by_service, spike_hits
from .weight_io import load_bytes, save_bytes

log = logging.getLogger(__name__)

_ON = frozenset({"1", "true", "yes", "on"})
FORMAT = "oncall-ranker-v1"
FEATURES = (
    "log_rise",
    "rise_ratio",
    "hour_sin",
    "hour_cos",
    "metric_score",
    "kind_jump",
    "kind_leak",
    "kind_rule",
    "kind_gap",
    "kind_alert",
    "severity_rank",
)


def learned_ranker_enabled() -> bool:
    return os.environ.get("LEARNED_RANKER", "").strip().lower() in _ON


def learned_ranker_path() -> Path | None:
    raw = os.environ.get("LEARNED_RANKER_PATH", "").strip()
    return Path(raw) if raw else None


def _version(dist_name: str) -> str | None:
    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return None


def xgboost_importable() -> bool:
    try:
        import xgboost  # noqa: F401
    except ImportError:
        return False
    return True


def torch_ranker_importable() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_backend(name: str | None) -> str:
    """``auto`` prefers XGBoost, then torch, then the pointwise logistic model."""
    asked = (name or "auto").strip().lower()
    if asked == "auto":
        if xgboost_importable():
            return "xgboost"
        if torch_ranker_importable():
            return "torch"
        return "pointwise"
    if asked not in ("xgboost", "torch", "pointwise"):
        raise ValueError(f"ranker backend must be auto, xgboost, torch or pointwise, not {name!r}")
    return asked


def _metric_score_at(
    series: list[tuple[pd.Timestamp, float]] | None,
    stamp: pd.Timestamp,
) -> float:
    """Latest model score at or before ``stamp``. Missing history is 0."""
    if not series:
        return 0.0
    best: float | None = None
    best_at: pd.Timestamp | None = None
    for when, score in series:
        if when <= stamp and (best_at is None or when >= best_at):
            best = score
            best_at = when
    return 0.0 if best is None or not math.isfinite(best) else float(best)


def candidate_frame(
    items: list[QueueItem],
    metric_scores: dict[str, list[tuple[pd.Timestamp, float]]] | None,
    config: Config,
) -> pd.DataFrame:
    """One row per item, same order. No fault id and no log text."""
    lookup = metric_scores or {}
    rows = []
    for item in items:
        rise = 0.0
        level = 0.0
        severity = 0
        if item.kind == "alert":
            severity = int(config.severity_rank.get(item.alert.severity, 0))
        elif item.kind != "gap":
            rise = float(item.spike.rise_at_confirm)
            level = float(item.spike.level_at_confirm)
        hour = item.shown_at.hour + item.shown_at.minute / 60
        safe_rise = max(rise, 1.0)
        rows.append(
            {
                "log_rise": math.log10(safe_rise) if item.kind not in ("alert", "gap") else 0.0,
                "rise_ratio": rise / level if level > 0 else 0.0,
                "hour_sin": math.sin(2 * math.pi * hour / 24),
                "hour_cos": math.cos(2 * math.pi * hour / 24),
                "metric_score": _metric_score_at(lookup.get(item.service), item.shown_at),
                "kind_jump": float(item.kind == "jump"),
                "kind_leak": float(item.kind == "leak"),
                "kind_rule": float(item.kind == "rule"),
                "kind_gap": float(item.kind == "gap"),
                "kind_alert": float(item.kind == "alert"),
                "severity_rank": float(severity),
            }
        )
    return pd.DataFrame(rows, columns=list(FEATURES))


def item_label(item: QueueItem, faults: list[Fault]) -> int:
    """1 when the existing catch rule would count this item. Gaps never count."""
    if item.kind == "gap":
        return 0
    known = {fault.fault_id for fault in faults}
    if item.kind == "alert":
        return int(item.alert.fault_id in known)
    return int(bool(spike_hits(item.spike, faults_by_service(faults))))


def caught_fault_ids(shown: list[QueueItem], faults: list[Fault]) -> set[str]:
    """Faults a shown queue catches, including an alert riding on a jump."""
    known = {fault.fault_id for fault in faults}
    by_service = faults_by_service(faults)
    caught: set[str] = set()
    for item in shown:
        if item.kind == "gap":
            continue
        if item.kind == "alert":
            if item.alert.fault_id in known:
                caught.add(item.alert.fault_id)
        else:
            caught.update(fault.fault_id for fault in spike_hits(item.spike, by_service) if fault.fault_id in known)
    return caught


def false_item_count(shown: list[QueueItem], faults: list[Fault]) -> int:
    by_service = faults_by_service(faults)
    known = {fault.fault_id for fault in faults}
    false_items = 0
    for item in shown:
        if item.kind == "gap" or item.attached:
            continue
        if item.kind == "alert":
            hit = item.alert.fault_id in known
        else:
            hit = any(fault.fault_id in known for fault in spike_hits(item.spike, by_service))
        if not hit:
            false_items += 1
    return false_items


def select_by_score(
    items: list[QueueItem],
    scores: np.ndarray | list[float],
    config: Config,
    threshold: float,
) -> list[QueueItem]:
    """First come, first served, but a score below ``threshold`` does not take a slot.

    Alert attachment and the alert and gap shares match ``build_queue``. A
    threshold below every score is that queue.
    """
    if len(scores) != len(items):
        raise ValueError("scores and items must be the same length")
    clones: list[QueueItem] = []
    score_of: dict[int, float] = {}
    for item, score in zip(items, scores):
        clone = QueueItem(item.shown_at, item.kind, item.service, item.payload)
        clones.append(clone)
        score_of[id(clone)] = float(score)
    return _fill(clones, score_of, config, threshold)


def _fill(
    items: list[QueueItem],
    score_of: dict[int, float],
    config: Config,
    threshold: float,
) -> list[QueueItem]:
    k = config.budget_k
    alert_cap = int(math.floor(k * config.alert_share))
    gap_cap = int(math.floor(k * config.gap_share))
    group = pd.Timedelta(minutes=config.group_minutes)
    by_day: dict[pd.Timestamp, list[QueueItem]] = {}
    for item in items:
        by_day.setdefault(day_of(item.shown_at, config), []).append(item)
    shown: list[QueueItem] = []
    running: dict[str, list] = {}
    last_group: QueueItem | None = None
    carry = 0
    days = sorted(by_day)
    all_days = pd.date_range(days[0], days[-1], freq="D") if days else []
    for day in all_days:
        today = k + carry
        used = alerts_used = gaps_used = 0
        ordered = sorted(by_day.get(day, []), key=lambda it: (it.shown_at, _KIND_ORDER.get(it.kind, 5)))
        for item in ordered:
            if item.kind == "alert":
                jumps = running.get(item.service, [])
                if any(jump.open_at(item.shown_at) for jump in jumps):
                    item.attached = True
                    shown.append(item)
                    continue
                if score_of[id(item)] < threshold or used >= today or alerts_used >= alert_cap:
                    continue
                alerts_used += 1
            elif item.kind in METRIC_KINDS:
                if (
                    config.group_minutes > 0
                    and last_group is not None
                    and item.service != last_group.service
                    and item.shown_at - last_group.shown_at <= group
                ):
                    item.attached = True
                    shown.append(item)
                    running.setdefault(item.service, []).append(item.spike)
                    continue
                if score_of[id(item)] < threshold or used >= today:
                    continue
            elif item.kind == "gap":
                if score_of[id(item)] < threshold or used >= today or gaps_used >= gap_cap:
                    continue
                gaps_used += 1
            elif score_of[id(item)] < threshold or used >= today:
                continue
            used += 1
            shown.append(item)
            if item.kind in METRIC_KINDS:
                running.setdefault(item.service, []).append(item.spike)
                last_group = item
        carry = min(config.carry_over_max, max(0, today - used)) if config.carry_over_max > 0 else 0
    return shown


def tune_score_threshold(
    items: list[QueueItem],
    scores: np.ndarray,
    faults: list[Fault],
    config: Config,
) -> float:
    """Lowest training cost. A tie keeps the higher threshold (closer to showing less)."""
    if len(items) == 0 or not faults:
        return 1.0
    grid = list(np.unique(np.quantile(np.asarray(scores, dtype=float), np.linspace(0, 0.95, 15))))
    grid.append(float(np.max(scores)) + 1.0)
    n_faults = len(faults)
    best: tuple[float, float] | None = None
    for threshold in grid:
        shown = select_by_score(items, scores, config, float(threshold))
        caught = len(caught_fault_ids(shown, faults))
        false_items = false_item_count(shown, faults)
        cost = config.miss_cost * (n_faults - caught) + config.false_alarm_cost * false_items
        if best is None or cost < best[0] - 1e-9 or (abs(cost - best[0]) <= 1e-9 and float(threshold) > best[1]):
            best = (cost, float(threshold))
    return 1.0 if best is None else best[1]


def tree_margins(trees: list[np.ndarray], base_score: float, features: np.ndarray) -> np.ndarray:
    dense = np.ascontiguousarray(features, dtype=np.float32)
    margin = np.full(dense.shape[0], float(base_score), dtype=np.float64)
    for tree in trees:
        margin += _tree_leaves(np.asarray(tree, dtype=NODE_DTYPE), dense)
    return margin


def mlp_scores(features: np.ndarray, weights: dict[str, np.ndarray]) -> np.ndarray:
    w1 = np.asarray(weights["w1"], dtype=np.float64)
    b1 = np.asarray(weights["b1"], dtype=np.float64)
    w2 = np.asarray(weights["w2"], dtype=np.float64)
    b2 = np.asarray(weights["b2"], dtype=np.float64)
    hidden = np.maximum(0.0, features @ w1.T + b1)
    return (hidden @ w2.T + b2).reshape(-1)


def _scale(features: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = features.mean(axis=0)
    scale = features.std(axis=0)
    scale = np.where(scale >= 1e-6, scale, 1.0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    scaled = (features - mean) / scale
    return np.nan_to_num(scaled, nan=0.0, posinf=0.0, neginf=0.0), mean, scale


@dataclass
class LearnedRanker:
    backend: str
    device: str
    threshold: float
    mean: np.ndarray
    scale: np.ndarray
    uses_metric_score: bool
    coef: np.ndarray | None = None
    intercept: float = 0.0
    trees: list[np.ndarray] | None = None
    base_score: float = 0.0
    mlp: dict[str, np.ndarray] | None = None
    versions: dict[str, str | None] = field(default_factory=dict)
    fell_back: bool = False

    def score_frame(self, frame: pd.DataFrame) -> np.ndarray:
        raw = frame[list(FEATURES)].to_numpy(dtype=np.float64)
        scaled = (raw - self.mean) / self.scale
        scaled = np.nan_to_num(scaled, nan=0.0, posinf=0.0, neginf=0.0)
        if self.backend == "pointwise":
            if self.coef is None:
                raise RuntimeError("pointwise ranker has no coefficients")
            logits = np.clip(scaled @ self.coef + self.intercept, -30, 30)
            return 1.0 / (1.0 + np.exp(-logits))
        if self.backend == "xgboost":
            if not self.trees:
                raise RuntimeError("xgboost ranker has no trees")
            return tree_margins(self.trees, self.base_score, scaled)
        if self.backend == "torch":
            if not self.mlp:
                raise RuntimeError("torch ranker has no weights")
            return mlp_scores(scaled, self.mlp)
        raise ValueError(f"unknown ranker backend {self.backend!r}")

    def select(
        self,
        items: list[QueueItem],
        metric_scores: dict[str, list[tuple[pd.Timestamp, float]]] | None,
        config: Config,
    ) -> list[QueueItem]:
        if not items:
            return []
        scores = self.score_frame(candidate_frame(items, metric_scores, config))
        return select_by_score(items, scores, config, self.threshold)

    def retune(
        self,
        items: list[QueueItem],
        faults: list[Fault],
        config: Config,
        metric_scores: dict[str, list[tuple[pd.Timestamp, float]]] | None,
    ) -> float:
        if not items:
            self.threshold = 1.0
            return self.threshold
        scores = self.score_frame(candidate_frame(items, metric_scores, config))
        self.threshold = tune_score_threshold(items, scores, faults, config)
        return self.threshold


def _training_rows(
    items: list[QueueItem],
    faults: list[Fault],
    metric_scores: dict[str, list[tuple[pd.Timestamp, float]]] | None,
    config: Config,
    train_end: pd.Timestamp,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, list[QueueItem]]:
    boundary = pd.Timestamp(train_end)
    if boundary.tzinfo is None:
        boundary = boundary.tz_localize("UTC")
    else:
        boundary = boundary.tz_convert("UTC")
    kept_items = [item for item in items if item.shown_at < boundary]
    kept_faults = [fault for fault in faults if fault.start < boundary]
    frame = candidate_frame(kept_items, metric_scores, config)
    labels = np.asarray([item_label(item, kept_faults) for item in kept_items], dtype=int)
    groups = np.asarray([day_of(item.shown_at, config).value for item in kept_items], dtype=np.int64)
    return frame, labels, groups, kept_items


def fit_ranker(
    items: list[QueueItem],
    faults: list[Fault],
    config: Config,
    metric_scores: dict[str, list[tuple[pd.Timestamp, float]]] | None,
    *,
    train_end: pd.Timestamp,
    backend: str = "pointwise",
    device: str = "cpu",
    seed: int = 42,
) -> tuple[LearnedRanker | None, str]:
    """Fit on items and faults strictly before ``train_end``. ``(model, status)``."""
    frame, labels, groups, kept = _training_rows(items, faults, metric_scores, config, train_end)
    if len(kept) < 2 or len(set(labels.tolist())) < 2:
        return None, "one-class"
    raw = frame.to_numpy(dtype=np.float64)
    scaled, mean, scale = _scale(raw)
    uses_metric = bool(np.any(frame["metric_score"].to_numpy() != 0))
    asked = resolve_backend(backend)
    used = asked
    fell_back = False
    coef = intercept = None
    trees = None
    base = 0.0
    mlp = None
    resolved_device = "cpu"
    if asked == "xgboost":
        try:
            from .ranker_xgb import fit_xgboost_ranker

            try:
                trees, base, resolved_device = fit_xgboost_ranker(
                    scaled, labels, groups, seed=seed, device=device
                )
            except Exception:
                if device.startswith("cuda"):
                    trees, base, resolved_device = fit_xgboost_ranker(
                        scaled, labels, groups, seed=seed, device="cpu"
                    )
                    fell_back = True
                else:
                    raise
        except Exception:
            log.warning("xgboost ranker failed; using the pointwise model", exc_info=True)
            used = "pointwise"
            fell_back = True
    if asked == "torch" and used == "torch":
        try:
            from .metric_tcn import resolve_torch_device
            from .ranker_torch import train_mlp

            resolved_device, torch_fallback = resolve_torch_device(device)
            fell_back = fell_back or torch_fallback
            mlp = train_mlp(
                scaled, labels, groups, hidden=8, epochs=8, seed=seed, device=resolved_device
            )
        except Exception:
            log.warning("torch ranker failed; using the pointwise model", exc_info=True)
            used = "pointwise"
            fell_back = True
            mlp = None
    if used == "pointwise":
        weights = np.where(labels == 1, config.miss_cost, config.false_alarm_cost)
        model = LogisticRegression(max_iter=500, random_state=seed)
        model.fit(scaled, labels, sample_weight=weights)
        coef = np.asarray(model.coef_[0], dtype=np.float64)
        intercept = float(model.intercept_[0])
        resolved_device = "cpu"
    ranker = LearnedRanker(
        backend=used,
        device=resolved_device,
        threshold=0.0,
        mean=mean,
        scale=scale,
        uses_metric_score=uses_metric,
        coef=coef,
        intercept=float(intercept or 0.0),
        trees=trees,
        base_score=float(base),
        mlp=mlp,
        fell_back=fell_back,
        versions={
            "sklearn": _version("scikit-learn"),
            "xgboost": _version("xgboost"),
            "torch": _version("torch"),
        },
    )
    scores = ranker.score_frame(frame)
    ranker.threshold = tune_score_threshold(kept, scores, [fault for fault in faults if fault.start < _as_utc(train_end)], config)
    return ranker, "trained"


def _as_utc(stamp: pd.Timestamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def _node_names() -> tuple[str, ...]:
    names = NODE_DTYPE.names
    if not names:
        raise RuntimeError("tree node dtype has no field names")
    return names


def _trees_payload(trees: list[np.ndarray] | None) -> list[dict[str, list]]:
    names = _node_names()
    payload = []
    for tree in trees or []:
        array = np.asarray(tree, dtype=NODE_DTYPE)
        payload.append({name: array[name].tolist() for name in names})
    return payload


def _trees_from_payload(payload: list[dict]) -> list[np.ndarray]:
    names = _node_names()
    fields = NODE_DTYPE.fields
    if fields is None:
        raise RuntimeError("tree node dtype has no fields")
    trees = []
    for raw in payload:
        array = np.zeros(len(raw["feature"]), dtype=NODE_DTYPE)
        for name in names:
            array[name] = np.asarray(raw[name], dtype=fields[name][0])
        trees.append(array)
    return trees


def save_ranker(ranker: LearnedRanker, path: Path) -> str:
    payload = {
        "format": FORMAT,
        "pages": False,
        "backend": ranker.backend,
        "device": ranker.device,
        "fell_back": ranker.fell_back,
        "threshold": ranker.threshold,
        "features": list(FEATURES),
        "mean": [float(value) for value in ranker.mean],
        "scale": [float(value) for value in ranker.scale],
        "uses_metric_score": ranker.uses_metric_score,
        "coef": None if ranker.coef is None else [float(value) for value in ranker.coef],
        "intercept": ranker.intercept,
        "base_score": ranker.base_score,
        "trees": _trees_payload(ranker.trees),
        "mlp": None
        if not ranker.mlp
        else {key: np.asarray(value, dtype=np.float64).tolist() for key, value in ranker.mlp.items()},
        "versions": ranker.versions,
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return save_bytes(path, blob)


def load_ranker(path: Path, expected_sha: str | None = None) -> tuple[LearnedRanker | None, str]:
    pinned = expected_sha if expected_sha is not None else os.environ.get("LEARNED_RANKER_SHA256", "")
    blob, status = load_bytes(path, pinned or None)
    if blob is None:
        return None, status
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "bad-format"
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        return None, "bad-format"
    if list(payload.get("features") or []) != list(FEATURES):
        return None, "bad-format"
    coef = payload.get("coef")
    mlp = payload.get("mlp")
    ranker = LearnedRanker(
        backend=str(payload.get("backend") or "pointwise"),
        device=str(payload.get("device") or "cpu"),
        threshold=float(payload.get("threshold") or 0.0),
        mean=np.asarray(payload.get("mean") or [], dtype=np.float64),
        scale=np.asarray(payload.get("scale") or [], dtype=np.float64),
        uses_metric_score=bool(payload.get("uses_metric_score", False)),
        coef=None if coef is None else np.asarray(coef, dtype=np.float64),
        intercept=float(payload.get("intercept") or 0.0),
        trees=_trees_from_payload(payload.get("trees") or []),
        base_score=float(payload.get("base_score") or 0.0),
        mlp=None if not mlp else {key: np.asarray(value, dtype=np.float64) for key, value in mlp.items()},
        versions={str(key): value for key, value in (payload.get("versions") or {}).items()},
        fell_back=bool(payload.get("fell_back", False)),
    )
    return ranker, "ok"
