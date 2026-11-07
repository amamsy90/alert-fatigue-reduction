"""Explain the learned ranker, and measure confidence and disagreement abstention.

The three production flags stay off. This script does not page and does not
turn ``RANKER_EXPLAIN``, ``RANKER_ABSTAIN``, ``RANKER_DISAGREE`` or
``LEARNED_RANKER`` on. The coverage curve is a hypothetical policy: it is not
the queue the product shows.

Train on the first week. Score the later days. ``--walk-forward`` refits on
the past only, one week at a time, at K = 20. A variant is described with the
same 95% bootstrap interval the queue uses. This audit does not keep a model:
``flags_changed`` stays false even when an interval is above zero.

GAIA is used only when both folders exist. A synthetic run leaves ``gaia`` as
pending and does not fill in GAIA counts.

    cd app
    uv run python run_ranker_audit.py --data synthetic --backend pointwise --seeds 42,0,1 --walk-forward \\
        --out results/ranker_audit_synthetic.json

On the GPU machine, with ``data/gaia_raw`` and ``data/gaia_metrics`` present:

    cd app
    uv run --no-sync python run_ranker_audit.py --data gaia --backend xgboost --device cuda \\
        --seeds 42,0,1,2,3 --walk-forward --out results/ranker_audit_gaia.json

Add ``--encoder results/log_encoder`` when a hash-checked contrastive encoder
is already trained. Without it the second opinion is the first-come queue.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from experiments.model_ablation.model_eval import published_bounds
from src.baseline import baseline_decisions
from src.classifier import AlertClassifier
from src.config import Config, day_of
from src.eval_stats import bootstrap_mean_ci
from src.experiment import load_gaia
from src.features import build_feature_frame
from src.memory_spikes import detect_gaps, detect_jumps, detect_leaks
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold
from src.queue import (
    QueueItem,
    alert_items,
    build_queue,
    n_days_between,
    score_queue,
    spike_items,
    tune_confirm_thresholds,
)
from src.ranker import FEATURES, caught_fault_ids, fit_ranker
from src.ranker_audit import (
    bootstrap_rate_difference,
    choose_operating_points,
    coverage_curve,
    default_abstain_delta,
    fault_disagreement,
    interventional_shap,
    live_review_mask,
    round_point,
    score_candidates,
    top_reasons,
)
from src.sample_data import generate_multivariate, load_multivariate
from src.tracking import record_run


def _utc(stamp: pd.Timestamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def _gaia_ready() -> bool:
    return Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()


def _spearman(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) < 2:
        return None

    def ranks(values: np.ndarray) -> np.ndarray:
        order = np.argsort(values, kind="mergesort")
        out = np.empty(len(values), dtype=float)
        out[order] = np.arange(len(values), dtype=float)
        return out

    a = ranks(np.asarray(left, dtype=float))
    b = ranks(np.asarray(right, dtype=float))
    if float(a.std()) == 0.0 or float(b.std()) == 0.0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _arrival_spearman(items: list[QueueItem], scores: np.ndarray, config: Config) -> float | None:
    by_day: dict[pd.Timestamp, list[int]] = {}
    for index, item in enumerate(items):
        by_day.setdefault(day_of(item.shown_at, config), []).append(index)
    values = []
    for indexes in by_day.values():
        if len(indexes) < 2:
            continue
        ordered = sorted(indexes, key=lambda i: (items[i].shown_at, i))
        arrival = np.arange(len(ordered), dtype=float)
        ranker_scores = np.asarray([scores[i] for i in ordered], dtype=float)
        value = _spearman(arrival, ranker_scores)
        if value is not None:
            values.append(value)
    if not values:
        return None
    return float(np.mean(values))


def _round_diff(block: dict) -> dict:
    out = dict(block)
    for key in ("rate_flagged", "rate_other"):
        if out.get(key) is not None:
            out[key] = round(float(out[key]), 4)
    diff = out.get("difference")
    if isinstance(diff, dict):
        out["difference"] = {
            "mean": round(float(diff["mean"]), 4),
            "low": round(float(diff["low"]), 4),
            "high": round(float(diff["high"]), 4),
            "n": diff["n"],
            "draws": diff["draws"],
        }
    return out


def _candidates(
    metrics: pd.DataFrame,
    alerts: list,
    faults: list,
    config: Config,
    *,
    train_end: pd.Timestamp | None,
    test_end: pd.Timestamp | None,
) -> dict:
    frame = metrics.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    origin, default_stop, window_end = published_bounds(frame, alerts)
    boundary = _utc(train_end) if train_end is not None else origin + pd.Timedelta(days=config.split_day)
    if test_end is not None:
        stop = _utc(test_end)
        window_end = "caller"
    else:
        stop = default_stop
    frame = frame[frame["timestamp"] < stop]
    train_alerts = [alert for alert in alerts if alert.timestamp < boundary]
    test_alerts = [alert for alert in alerts if boundary <= alert.timestamp < stop]
    train_faults = [fault for fault in faults if fault.start < boundary]
    test_faults = [fault for fault in faults if boundary <= fault.start < stop]
    train_jumps = detect_jumps(frame, config, detect_until=boundary)
    test_jumps = detect_jumps(frame, config, detect_from=boundary, detect_until=stop)
    train_gaps = detect_gaps(frame, config, detect_until=boundary)
    test_gaps = detect_gaps(frame, config, detect_from=boundary, detect_until=stop)
    train_leaks = detect_leaks(frame, config, detect_until=boundary)
    test_leaks = detect_leaks(frame, config, detect_from=boundary, detect_until=stop)
    thr, by_series = tune_confirm_thresholds(train_jumps, train_faults, config, train_until=boundary)
    tuned = replace(config, confirm_min_rise=thr, confirm_min_rise_by_series=by_series)
    alert_model = None
    alert_threshold = None
    try:
        if train_alerts:
            alert_model = AlertClassifier(tuned).fit(train_alerts, train_jumps)
            alert_threshold = tune_threshold(
                train_alerts, alert_model.predict_proba(train_alerts, train_jumps), tuned
            )
    except ValueError:
        alert_model = None
    if alert_model is not None and alert_threshold is not None and test_alerts:
        proba = alert_model.predict_proba(test_alerts, test_jumps)
        flags = (
            [bool(value) for value in build_feature_frame(test_alerts, tuned, test_jumps)["during_memory_spike"]]
            if test_jumps
            else [False] * len(test_alerts)
        )
        test_decisions = spike_review_decisions(
            test_alerts, proba, tuned, alert_threshold, bayes_threshold(tuned), flags
        )
        train_proba = alert_model.predict_proba(train_alerts, train_jumps)
        train_flags = (
            [bool(value) for value in build_feature_frame(train_alerts, tuned, train_jumps)["during_memory_spike"]]
            if train_jumps
            else [False] * len(train_alerts)
        )
        train_decisions = spike_review_decisions(
            train_alerts, train_proba, tuned, alert_threshold, bayes_threshold(tuned), train_flags
        )
    else:
        test_decisions = baseline_decisions(test_alerts, tuned)
        train_decisions = baseline_decisions(train_alerts, tuned)
    train_items = (
        spike_items(train_jumps, tuned)
        + spike_items(train_leaks, tuned)
        + spike_items(train_gaps, tuned)
        + alert_items(train_alerts, train_decisions)
    )
    test_items = (
        spike_items(test_jumps, tuned)
        + spike_items(test_leaks, tuned)
        + spike_items(test_gaps, tuned)
        + alert_items(test_alerts, test_decisions)
    )
    return {
        "frame": frame,
        "tuned": tuned,
        "boundary": boundary,
        "stop": stop,
        "window_end": window_end,
        "train_items": train_items,
        "test_items": test_items,
        "train_faults": train_faults,
        "test_faults": test_faults,
        "n_days": n_days_between(boundary, stop, tuned),
    }


def _examples(ranker, items, view, faults) -> tuple[list[dict], float | None, list[dict]]:
    """Three worked rows and global importance. Labels are scoring-only."""
    scores = view["scores"]
    attached = view["attached"]
    usable = [index for index, flag in enumerate(attached) if not flag]
    if not usable:
        return [], None, []
    order = np.asarray(usable)
    high = int(order[np.argmax(scores[order])])
    low = int(order[np.argmin(scores[order])])
    near = int(order[np.argmin(np.abs(scores[order] - view["threshold"]))])
    picked = list(dict.fromkeys([high, low, near]))
    frame = view["frame"]
    raw = frame[list(FEATURES)].to_numpy(dtype=np.float64)
    scaled = (raw - ranker.mean) / ranker.scale
    scaled = np.nan_to_num(scaled, nan=0.0, posinf=0.0, neginf=0.0)
    phi, baseline = interventional_shap(ranker.score_matrix, scaled[picked])
    gap = float(np.max(np.abs(phi.sum(axis=1) + baseline - scores[picked])))
    # Importance uses every non-attached row when there are at most 512.
    # A larger test set uses the first 512 non-attached rows and says so.
    pool = order[:512]
    phi_all, _base = interventional_shap(ranker.score_matrix, scaled[pool])
    mean_abs = np.mean(np.abs(phi_all), axis=0)
    importance = [
        {"feature": FEATURES[int(index)], "mean_abs_contribution": round(float(mean_abs[index]), 6)}
        for index in np.argsort(-mean_abs)
    ]
    labels = ("highest score", "lowest score", "closest to the threshold")
    examples = []
    for slot, (index, label) in enumerate(zip(picked, labels)):
        item = items[index]
        examples.append(
            {
                "label": label,
                "kind": item.kind,
                "service": item.service,
                "shown_at": pd.Timestamp(item.shown_at).isoformat(),
                "score": round(float(scores[index]), 6),
                "threshold": view["threshold"],
                "in_queue": bool(view["in_queue"][index]),
                "catches_fault": bool(view["catches"][index]),
                "catches_fault_note": "scoring label from the catch rule; not a ranker feature",
                "reasons": top_reasons(phi[slot], raw[index], FEATURES),
            }
        )
    del faults
    return examples, gap, importance


def _k_report(ranker, prepared: dict, budget: int, *, explain: bool, encoder, n_draw: int) -> dict:
    cfg = replace(prepared["tuned"], budget_k=budget)
    model = replace(ranker)
    model.retune(prepared["train_items"], prepared["train_faults"], cfg, None)
    shown = model.select(prepared["test_items"], None, cfg)
    baseline = build_queue(
        [QueueItem(item.shown_at, item.kind, item.service, item.payload) for item in prepared["test_items"]],
        cfg,
    )
    test_faults = prepared["test_faults"]
    n_days = prepared["n_days"]
    ranker_row = score_queue("ranker", shown, test_faults, n_days, cfg)
    base_row = score_queue("baseline", baseline, test_faults, n_days, cfg)
    view = score_candidates(
        model,
        prepared["test_items"],
        shown,
        None,
        cfg,
        test_faults,
        encoder=encoder,
    )
    delta = default_abstain_delta(view["scores"], view["threshold"])
    review = live_review_mask(view["scores"], view["in_queue"], view["attached"], view["threshold"], delta)
    review_items = [item for item, flag in zip(prepared["test_items"], review) if flag]
    review_faults = caught_fault_ids(review_items, test_faults)
    queue_faults = caught_fault_ids(shown, test_faults)
    base_faults = caught_fault_ids(baseline, test_faults)
    curve = [round_point(point) for point in coverage_curve(
        view["scores"], view["in_queue"], view["attached"], view["catches"], view["threshold"]
    )]
    points = [round_point(point) for point in choose_operating_points(curve)]
    association = bootstrap_rate_difference(view["catches"], view["disagree"], n_draw=n_draw, seed=42)
    fault_ids = [fault.fault_id for fault in test_faults]
    if fault_ids:
        per_fault = [float(fid in queue_faults) - float(fid in base_faults) for fid in fault_ids]
        interval = bootstrap_mean_ci(per_fault, n_draw=n_draw, seed=42)
        kept = interval.low > 0
        catch_delta = {
            "mean": round(interval.mean, 4),
            "low": round(interval.low, 4),
            "high": round(interval.high, 4),
            "n": interval.n,
            "decision": "keep" if kept else "drop",
            "reason": (
                "95% interval of the per-fault catch difference vs the first-come queue is above 0"
                if kept
                else "95% interval of the per-fault catch difference vs the first-come queue is not above 0"
            ),
        }
    else:
        catch_delta = {"decision": "skipped", "reason": "no held-out faults", "per_fault_catch_delta": None}
    examples: list[dict] = []
    importance: list[dict] = []
    sum_gap = None
    if explain:
        examples, sum_gap, importance = _examples(model, prepared["test_items"], view, test_faults)
    covered = fault_disagreement(prepared["test_items"], test_faults, view["disagree"], queue_faults)
    return {
        "budget_k": budget,
        "threshold": view["threshold"],
        "candidates": len(prepared["test_items"]),
        "slot_items": int(view["in_queue"].sum()),
        "baseline_slot_items": int(view["baseline_in"].sum()),
        "ranker_queue": {
            "faults_caught": ranker_row["faults_caught"],
            "faults": ranker_row["faults"],
            "detection_rate": ranker_row["detection_rate"],
            "false_items_per_day": ranker_row["false_items_per_day"],
            "precision": ranker_row["precision"],
        },
        "baseline_queue": {
            "faults_caught": base_row["faults_caught"],
            "faults": base_row["faults"],
            "detection_rate": base_row["detection_rate"],
            "false_items_per_day": base_row["false_items_per_day"],
            "precision": base_row["precision"],
        },
        "per_fault_catch_delta_vs_baseline": catch_delta,
        "shipped_abstain_preview": {
            "note": (
                "Items the RANKER_ABSTAIN flag would list: not shown, and not confidently below the gate. "
                "The flag does not remove anything from the K queue. This preview uses the 25th percentile "
                "of |score - threshold| unless RANKER_ABSTAIN_DELTA is set. The curve below is hypothetical."
            ),
            "delta": round(float(delta), 6),
            "review_count": int(review.sum()),
            "review_faults": len(review_faults),
            "review_faults_missed_by_queue": len(review_faults - queue_faults),
        },
        "hypothetical_coverage_curve": curve,
        "operating_points": points,
        "disagreement": {
            "partner": view["partner"],
            "delta": round(float(view["disagree_delta"]), 6),
            "count": int(view["disagree"].sum()),
            "in_queue": int((view["disagree"] & view["in_queue"]).sum()),
            "dropped": int((view["disagree"] & ~view["in_queue"] & ~view["attached"]).sum()),
            "item_catch_rate": _round_diff(association),
            "faults": covered,
            "arrival_score_spearman_mean": _arrival_spearman(prepared["test_items"], view["scores"], cfg),
            "note": (
                "Strong disagreement is a slot-membership difference whose |score - threshold| is at least "
                "the 75th percentile of those gaps. Alerts use the contrastive encoder's median split when "
                "an encoder is loaded; other items, and every item when it is not, use the first-come queue. "
                "An in-queue flag is not removed from the page set."
            ),
        },
        "examples": examples,
        "global_importance": importance,
        "contribution_sum_gap": None if sum_gap is None else round(float(sum_gap), 8),
        "importance_rows": None if not explain else min(512, int((~view["attached"]).sum())),
        "pages": False,
    }


def _fit_report(prepared: dict, *, backend: str, device: str, seed: int, explain: bool, encoder, budgets: tuple[int, ...], n_draw: int) -> dict:
    cfg = replace(prepared["tuned"], budget_k=budgets[0])
    ranker, status = fit_ranker(
        prepared["train_items"],
        prepared["train_faults"],
        cfg,
        None,
        train_end=prepared["boundary"],
        backend=backend,
        device=device,
        seed=seed,
    )
    if ranker is None:
        return {"seed": seed, "status": status, "by_k": {}}
    by_k = {}
    for index, budget in enumerate(budgets):
        by_k[str(budget)] = _k_report(
            ranker,
            prepared,
            budget,
            explain=explain and index == 0,
            encoder=encoder,
            n_draw=n_draw,
        )
    return {
        "seed": seed,
        "status": status,
        "backend": ranker.backend,
        "device": ranker.device,
        "fell_back": ranker.fell_back,
        "by_k": by_k,
    }


def _walk(metrics, alerts, faults, config: Config, *, backend: str, device: str, seeds: list[int], encoder, n_draw: int) -> dict:
    origin, end, _how = published_bounds(metrics, alerts)
    test_start = origin + pd.Timedelta(days=config.split_day)
    folds = []
    while test_start < end:
        test_stop = min(test_start + pd.Timedelta(days=7), end)
        in_week = [fault for fault in faults if test_start <= fault.start < test_stop]
        if not in_week:
            break
        prepared = _candidates(metrics, alerts, faults, config, train_end=test_start, test_end=test_stop)
        seed_rows = []
        for seed in seeds:
            report = _fit_report(
                prepared,
                backend=backend,
                device=device,
                seed=seed,
                explain=False,
                encoder=encoder,
                budgets=(20,),
                n_draw=n_draw,
            )
            block = (report.get("by_k") or {}).get("20")
            seed_rows.append(
                {
                    "seed": seed,
                    "status": report.get("status"),
                    "faults_caught": None if block is None else block["ranker_queue"]["faults_caught"],
                    "baseline_caught": None if block is None else block["baseline_queue"]["faults_caught"],
                    "review_count": None if block is None else block["shipped_abstain_preview"]["review_count"],
                    "disagreement_count": None if block is None else block["disagreement"]["count"],
                    "per_fault_catch_delta_vs_baseline": None if block is None else block["per_fault_catch_delta_vs_baseline"],
                }
            )
        folds.append(
            {
                "train_end": prepared["boundary"].isoformat(),
                "test_end": prepared["stop"].isoformat(),
                "test_faults": len(prepared["test_faults"]),
                "seeds": seed_rows,
            }
        )
        test_start = test_stop
    return {"folds": folds, "flags_changed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ranker explanations, confidence curve, disagreement.")
    parser.add_argument("--data", choices=("auto", "synthetic", "gaia"), default="auto")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--backend", default="pointwise")
    parser.add_argument("--seeds", default="42,0,1")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--encoder", default=None, help="contrastive encoder directory or weights file")
    parser.add_argument("--out", default=None)
    parser.add_argument("--n-draw", type=int, default=4000)
    args = parser.parse_args()
    which = args.data
    if which == "auto":
        which = "gaia" if _gaia_ready() else "synthetic"
    if which == "gaia" and not _gaia_ready():
        raise SystemExit(
            "GAIA folders are not in this run (data/gaia_raw and data/gaia_metrics). Nothing was scored."
        )
    if which == "gaia":
        data = load_gaia()
        metrics, alerts, faults = data.metrics, data.alerts, data.faults
    else:
        folder = Path(tempfile.mkdtemp(prefix="ranker-audit-"))
        generate_multivariate(folder, seed=42, days=21 if args.walk_forward else 14, freq="1min")
        metrics, alerts, faults = load_multivariate(folder)
    seeds = [int(part) for part in str(args.seeds).split(",") if part.strip()]
    if not seeds:
        raise SystemExit("Pass at least one --seeds value")
    encoder = None
    encoder_status = "not requested"
    if args.encoder:
        from src.log_encoder import load_contrastive_encoder

        encoder, error = load_contrastive_encoder(args.encoder)
        encoder_status = "loaded" if encoder is not None else f"baseline ({error})"
        if encoder is None:
            print(f"encoder not used: {error}", file=sys.stderr)
    prepared = _candidates(metrics, alerts, faults, Config(), train_end=None, test_end=None)
    by_seed = []
    for seed in seeds:
        by_seed.append(
            _fit_report(
                prepared,
                backend=args.backend,
                device=args.device,
                seed=seed,
                explain=seed == seeds[0],
                encoder=encoder,
                budgets=(20, 1),
                n_draw=args.n_draw,
            )
        )
    primary = by_seed[0]
    gap = None
    block = (primary.get("by_k") or {}).get("20")
    if block is not None:
        gap = block.get("contribution_sum_gap")
    report = {
        "dataset": which,
        "gaia": "pending: this run did not score GAIA" if which != "gaia" else "scored",
        "pages": False,
        "flags_changed": False,
        "flags_default": "off",
        "flags_reason": (
            "This audit does not set LEARNED_RANKER, RANKER_EXPLAIN, RANKER_ABSTAIN or RANKER_DISAGREE. "
            "A keep interval on this file would still leave the flags off. GAIA is required before any "
            "claim about the published queue."
        ),
        "method": {
            "explain": (
                "Exact interventional SHAP against the training mean. The shap package is not installed. "
                "Stored trees have no booster, so XGBoost pred_contribs is not called. Contributions sum "
                "to the ranker score."
            ),
            "abstain": (
                "Shipped preview: a candidate that was not shown and whose score is at least "
                "threshold minus the 25th percentile of |score - threshold|. Hypothetical curve: "
                "automated keep when the item is in the ranker queue and score >= threshold + delta; "
                "automated drop when it is not and score <= threshold - delta; otherwise review."
            ),
            "disagree": (
                "Second opinion is the contrastive encoder median split for alerts when --encoder loads, "
                "otherwise the first-come queue. Strong means |score - threshold| is at least the 75th "
                "percentile. In-queue flags stay in the page set."
            ),
            "split": "first week train, later days test",
            "bootstrap_seed": 42,
            "n_draw": args.n_draw,
        },
        "encoder": encoder_status,
        "train_end": prepared["boundary"].isoformat(),
        "test_end": prepared["stop"].isoformat(),
        "window_end": prepared["window_end"],
        "test_faults": len(prepared["test_faults"]),
        "seeds": seeds,
        "by_seed": by_seed,
        "walk_forward": None,
        "command": " ".join(sys.argv),
    }
    if args.walk_forward:
        report["walk_forward"] = _walk(
            metrics,
            alerts,
            faults,
            Config(),
            backend=args.backend,
            device=args.device,
            seeds=seeds,
            encoder=encoder,
            n_draw=args.n_draw,
        )
    out = Path(args.out) if args.out else Path("results") / f"ranker_audit_{which}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "dataset": report["dataset"],
        "gaia": report["gaia"],
        "out": str(out),
        "test_faults": report["test_faults"],
        "contribution_sum_gap": gap,
        "flags_changed": False,
    }, indent=2))
    for seed_row in by_seed:
        block20 = (seed_row.get("by_k") or {}).get("20")
        if block20 is None:
            print(f"seed {seed_row['seed']}: {seed_row.get('status')}")
            continue
        print(
            f"seed {seed_row['seed']} K=20 caught {block20['ranker_queue']['faults_caught']}"
            f"/{block20['ranker_queue']['faults']} baseline {block20['baseline_queue']['faults_caught']}"
            f" review {block20['shipped_abstain_preview']['review_count']}"
            f" disagree {block20['disagreement']['count']}"
            f" decision {block20['per_fault_catch_delta_vs_baseline'].get('decision')}"
        )
    record_run(
        f"ranker_audit_{which}",
        config=Config(),
        outputs=[out],
        metrics={"dataset": which, "test_faults": report["test_faults"], "pages": 0},
        note=report["gaia"],
    )
    if gap is not None and gap > 1e-4:
        raise SystemExit(f"SHAP contributions do not sum to the ranker score (gap {gap})")


if __name__ == "__main__":
    main()
