"""Scheduled retraining: decide if it is due, retrain through the gate, verify, roll back on failure.

Run it from a scheduler (cron, a Kubernetes CronJob, or `POST /models/retrain`):

    python -m product.cli retrain            # does nothing unless a retrain is due
    python -m product.cli retrain --force    # retrain now (the cooldown and the gate still apply)

The steps, each one a safeguard:

1. Due? A retrain needs enough new Real marks (MIN_NEW_REAL_MARKS), or an old model
   (RETRAIN_AFTER_DAYS) with at least one new mark. Not twice within MIN_HOURS_BETWEEN.
2. Data: the alerts stored by recent jobs, labelled from the on-call marks. Too small or
   one-class history is not trained on.
3. One job at a time: a scoring job in progress is never interrupted.
4. The same promotion gate as every other job decides whether the new model becomes active.
   A refused model stays a candidate (and is tallied by the shadow challenger).
5. Verification: the promoted model is read back from disk through the hash and allowlist
   checks and must score real alerts into valid probabilities. If not, the previous
   active version is restored automatically and the card says so.
6. Every model card records the trigger, the reasons, the data fingerprint, the code commit
   and the library versions, so a model can be traced to exactly what built it.

Nothing here runs by itself inside the web process: a scheduler calls it, so a retrain can
be paused by pausing the scheduler.
"""
from __future__ import annotations

import math

import pandas as pd

from src.entities import Alert

from .inputs import load_alerts_from_dicts
from .labels import apply_feedback
from .model_store import (
    RETRAIN_AFTER_DAYS,
    SavedModel,
    activate_version,
    annotate_version,
    deactivate_active,
    load_model,
    load_registry,
)
from .monitoring import METRICS
from .pipeline import _JOB_LOCK, _save_trained_model, score_alerts
from .settings import config_from_env
from .store import QueueStore

MIN_NEW_REAL_MARKS = 5
MIN_HOURS_BETWEEN = 6.0
MAX_HISTORY_JOBS = 60
MIN_ALERTS = 10


def retrain_decision(
    loaded: SavedModel | None,
    real_marks: int,
    registry: dict,
    *,
    now: pd.Timestamp,
    force: bool = False,
) -> dict:
    """Is a retrain due, and does anything block it? Pure, so the policy can be tested."""
    reasons: list[str] = []
    blockers: list[str] = []
    new_marks = real_marks - (loaded.feedback_real if loaded is not None else 0)
    if loaded is None:
        if real_marks >= MIN_NEW_REAL_MARKS:
            reasons.append(f"no model yet and {real_marks} Real marks")
    else:
        age_days = (now - pd.Timestamp(loaded.trained_at)).total_seconds() / 86400
        if new_marks >= MIN_NEW_REAL_MARKS:
            reasons.append(f"{new_marks} new Real marks since the model was trained")
        elif age_days >= RETRAIN_AFTER_DAYS and new_marks >= 1:
            reasons.append(f"model is {age_days:.0f} days old and has {new_marks} new Real mark(s)")
    if force:
        reasons.append("forced")
    versions = registry.get("versions", [])
    if versions:
        last = max(pd.Timestamp(v["trained_at"]) for v in versions)
        hours = (now - last).total_seconds() / 3600
        if hours < MIN_HOURS_BETWEEN:
            blockers.append(f"the last model was trained {hours:.1f} h ago (minimum {MIN_HOURS_BETWEEN:g} h)")
    return {"due": bool(reasons) and not blockers, "reasons": reasons, "blockers": blockers,
            "new_real_marks": max(new_marks, 0)}


def smoke_test(alerts: list[Alert]) -> tuple[bool, str]:
    """Read the active model back from disk and score real alerts with it.

    This goes through the same hash and allowlist checks as a live job, so it catches a
    file that was written badly as well as a model that cannot predict.
    """
    saved = load_model()
    if saved is None or saved.model is None:
        return False, "the active model file could not be loaded"
    sample = alerts[-20:]
    try:
        proba = saved.model.predict_proba(sample, [])
    except Exception as exc:  # noqa: BLE001 — any failure means the model is not safe to leave active
        return False, f"scoring failed: {type(exc).__name__}: {exc}"
    values = [float(p) for p in proba]
    if len(values) != len(sample) or any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in values):
        return False, "scores are not valid probabilities"
    return True, f"scored {len(values)} alerts"


def _skip(reason: str, **extra) -> dict:
    METRICS.inc("oncall_retrain_total", {"outcome": "skipped"}, doc="Retrain runs by outcome")
    return {"ran": False, "reason": reason, **extra}


def run_retrain(store: QueueStore, *, trigger: str = "scheduled", force: bool = False,
                now: pd.Timestamp | None = None) -> dict:
    """One scheduled retrain. Returns a report; never raises for an ordinary skip."""
    now = now or pd.Timestamp.now("UTC")
    loaded = load_model()
    real_marks = store.feedback_summary()["real"]
    registry = load_registry()
    decision = retrain_decision(loaded, real_marks, registry, now=now, force=force)
    if not decision["due"]:
        why = "; ".join(decision["blockers"]) or "not due: not enough new marks"
        return _skip(why, **decision)

    alerts = load_alerts_from_dicts(store.history_alerts(MAX_HISTORY_JOBS))
    apply_feedback(alerts, store)
    labelled = sum(1 for a in alerts if a.fault_id is not None)
    if len(alerts) < MIN_ALERTS or labelled == 0 or labelled == len(alerts):
        return _skip(f"history is not trainable: {len(alerts)} alerts, {labelled} labelled as faults", **decision)

    if not _JOB_LOCK.acquire(blocking=False):
        return _skip("a scoring job is running; try again shortly", **decision)
    try:
        result = score_alerts(alerts, budget_k=20, saved=loaded, config=config_from_env(20))
        previous = registry.get("active")
        provenance = {"trigger": trigger, "reasons": decision["reasons"], "history_alerts": len(alerts),
                      "fault_alerts": labelled, "previous_active": previous, "ran_at": now.isoformat()}
        _save_trained_model(result, real_marks, loaded, provenance)
    finally:
        _JOB_LOCK.release()

    training = result.training or {}
    version = training.get("model_version")
    if version is None:
        return _skip("the history did not have two label classes in its early window", **decision)
    promoted = bool(training.get("promoted"))
    report: dict = {"ran": True, "trigger": trigger, "reasons": decision["reasons"], "version": version,
                    "promoted": promoted, "gate": training.get("gate"), "previous_active": previous,
                    "history_alerts": len(alerts), "fault_alerts": labelled,
                    "data_fingerprint": result.data_fingerprint, "rolled_back": False}
    outcome = "refused"
    if promoted:
        ok, detail = smoke_test(alerts)
        report["verification"] = {"ok": ok, "detail": detail}
        annotate_version(version, verification=report["verification"])
        outcome = "promoted"
        if not ok:
            report["rolled_back"] = True
            outcome = "rolled_back"
            if previous:
                activate_version(previous)
            else:
                deactivate_active()  # nothing to go back to: live jobs use rules only
            report["active_after"] = previous
    report.setdefault("active_after", load_registry().get("active"))
    METRICS.inc("oncall_retrain_total", {"outcome": outcome}, doc="Retrain runs by outcome")
    return report
