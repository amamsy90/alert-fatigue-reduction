"""Ranker explanations, confidence review and disagreement. Flags default off."""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from product.cli import _attach_ranker_notes
from product.inputs import JobInputs
from product.pagerduty import page_items, would_have_paged
from product.pipeline import _job_meta, score_alerts
from src.accel import NODE_DTYPE
from src.config import Config
from src.entities import Alert
from src.memory_spikes import Spike
from src.optional_models import finish_queue
from src.queue import QueueItem
from src.ranker import FEATURES, LearnedRanker, candidate_frame, save_ranker
from src.ranker_audit import (
    audit_enabled,
    bootstrap_rate_difference,
    build_ranker_audit,
    choose_operating_points,
    disagreement_mask,
    hypothetical_point,
    interventional_shap,
    live_review_mask,
    ranker_abstain_enabled,
    ranker_disagree_enabled,
    ranker_explain_enabled,
)

T0 = pd.Timestamp("2026-03-01 00:00:00", tz="UTC")
ROOT = Path(__file__).parents[1]


def _jump(service: str, hour: int, rise: float) -> QueueItem:
    start = T0 + pd.Timedelta(hours=hour)
    spike = Spike(
        f"{service}:{hour}",
        service,
        start,
        start + pd.Timedelta(minutes=10),
        peak_rise=rise,
        confirm=start,
        rise_at_confirm=rise,
        level_at_confirm=1.0,
        kind="jump",
    )
    return QueueItem(start, "jump", service, spike)


def _alert(service: str, hour: int, message: str) -> QueueItem:
    stamp = T0 + pd.Timedelta(hours=hour)
    alert = Alert(f"a-{service}-{hour}", stamp, service, "log", "error", message)
    return QueueItem(stamp, "alert", service, alert)


def _pointwise(feature: str, weight: float, threshold: float) -> LearnedRanker:
    coef = np.zeros(len(FEATURES))
    coef[FEATURES.index(feature)] = weight
    return LearnedRanker(
        backend="pointwise",
        device="cpu",
        threshold=threshold,
        mean=np.zeros(len(FEATURES)),
        scale=np.ones(len(FEATURES)),
        uses_metric_score=False,
        coef=coef,
        intercept=0.0,
    )


def test_shap_sums_to_the_score_and_a_baseline_feature_adds_nothing():
    """FR-27. Exact interventional SHAP. A feature left at 0 does not move the score."""

    def predict(rows: np.ndarray) -> np.ndarray:
        return rows[:, 0] * 2.0 + rows[:, 1]

    rows = np.array([[1.0, 3.0, 0.0], [0.0, -1.0, 4.0], [0.0, 0.0, 0.0]])
    phi, baseline = interventional_shap(predict, rows)
    assert np.allclose(phi.sum(axis=1) + baseline, predict(rows))
    assert np.allclose(phi[:, 0], 2.0 * rows[:, 0])
    assert np.allclose(phi[:, 1], rows[:, 1])
    assert np.allclose(phi[0, 2], 0.0)
    assert np.allclose(phi[2], 0.0)


def test_tree_contributions_sum_to_the_margin():
    tree = np.zeros(3, dtype=NODE_DTYPE)
    tree[0]["feature"] = 0
    tree[0]["threshold"] = np.float32(0.0)
    tree[0]["yes"] = 1
    tree[0]["no"] = 2
    tree[0]["missing"] = 2
    tree[1]["feature"] = -1
    tree[1]["leaf"] = 1.0
    tree[2]["feature"] = -1
    tree[2]["leaf"] = -1.0
    ranker = LearnedRanker(
        backend="xgboost",
        device="cpu",
        threshold=0.0,
        mean=np.zeros(len(FEATURES)),
        scale=np.ones(len(FEATURES)),
        uses_metric_score=False,
        trees=[tree],
        base_score=0.1,
    )
    items = [_jump("api", 1, 2.0), _jump("db", 2, 50.0)]
    audit = build_ranker_audit(
        ranker,
        items,
        ranker.select(items, None, Config(budget_k=5)),
        None,
        Config(budget_k=5),
        explain=True,
        abstain=False,
        disagree=False,
        explain_max=10,
    )
    assert audit is not None
    assert audit["pages"] is False
    assert audit["contribution_sum_gap"] < 1e-6
    assert audit["queue_reasons"]
    assert audit["queue_reasons"][0]["reasons"][0]["feature"]


def test_the_three_flags_default_off_and_do_not_change_the_queue(monkeypatch, tmp_path):
    """FR-27. Unset flags leave the status dict and the shown items alone."""
    monkeypatch.delenv("RANKER_EXPLAIN", raising=False)
    monkeypatch.delenv("RANKER_ABSTAIN", raising=False)
    monkeypatch.delenv("RANKER_DISAGREE", raising=False)
    monkeypatch.delenv("LEARNED_RANKER", raising=False)
    assert ranker_explain_enabled() is False
    assert ranker_abstain_enabled() is False
    assert ranker_disagree_enabled() is False
    assert audit_enabled() is False
    items = [_jump("api", 1, 1.0), _jump("db", 2, 30.0)]
    monkeypatch.setenv("RANKER_EXPLAIN", "on")
    monkeypatch.setenv("RANKER_ABSTAIN", "on")
    monkeypatch.setenv("RANKER_DISAGREE", "on")
    shown, status = finish_queue(items, None, Config(budget_k=1))
    assert status == {"metric_model": "off", "learned_ranker": "off"}
    assert "ranker_audit" not in status
    assert [item.service for item in shown] == ["api"]

    ranker = _pointwise("log_rise", 1.0, 0.5)
    path = tmp_path / "ranker.json"
    save_ranker(ranker, path)
    monkeypatch.setenv("LEARNED_RANKER", "on")
    monkeypatch.setenv("LEARNED_RANKER_PATH", str(path))
    monkeypatch.delenv("RANKER_EXPLAIN", raising=False)
    monkeypatch.delenv("RANKER_ABSTAIN", raising=False)
    monkeypatch.delenv("RANKER_DISAGREE", raising=False)
    plain, plain_status = finish_queue(items, None, Config(budget_k=1))
    assert "ranker_audit" not in plain_status
    monkeypatch.setenv("RANKER_EXPLAIN", "on")
    monkeypatch.setenv("RANKER_ABSTAIN", "on")
    monkeypatch.setenv("RANKER_DISAGREE", "on")
    audited, audited_status = finish_queue(items, None, Config(budget_k=1))
    assert [item.payload for item in audited] == [item.payload for item in plain]
    audit = audited_status["ranker_audit"]
    assert audit["pages"] is False
    assert audit["needs_human_review"] == [] or all(row["pages"] is False for row in audit["needs_human_review"])
    shown_keys = {(item.kind, item.service, item.shown_at.isoformat()) for item in audited}
    for row in audit["needs_human_review"]:
        assert (row["kind"], row["service"], row["shown_at"]) not in shown_keys
        assert row["in_queue"] is False


def test_a_near_miss_is_listed_for_review_and_a_confident_drop_is_not():
    """FR-27. The review bucket is the silent drop the ranker is not sure about."""
    small, middle, large = _jump("api", 1, 1.0), _jump("api", 2, 10.0), _jump("api", 3, 100.0)
    items = [small, middle, large]
    ranker = _pointwise("log_rise", 1.0, 0.5)
    scores = ranker.score_frame(candidate_frame(items, None, Config()))
    ranker.threshold = float((scores[1] + scores[2]) / 2)
    delta = ranker.threshold - float((scores[0] + scores[1]) / 2)
    config = Config(budget_k=1)
    shown = ranker.select(items, None, config)
    assert [item.shown_at for item in shown] == [large.shown_at]
    audit = build_ranker_audit(
        ranker,
        items,
        shown,
        None,
        config,
        explain=True,
        abstain=True,
        disagree=False,
        abstain_delta=delta,
    )
    assert audit is not None and audit["pages"] is False
    assert audit["contribution_sum_gap"] < 1e-6
    assert audit["global_importance"][0]["feature"] == "log_rise"
    reviewed = {row["shown_at"] for row in audit["needs_human_review"]}
    assert middle.shown_at.isoformat() in reviewed
    assert small.shown_at.isoformat() not in reviewed
    assert large.shown_at.isoformat() not in reviewed
    middle_row = next(row for row in audit["needs_human_review"] if row["shown_at"] == middle.shown_at.isoformat())
    assert middle_row["review_reason"] == "low_confidence"
    both = build_ranker_audit(
        ranker,
        items,
        shown,
        None,
        config,
        explain=False,
        abstain=True,
        disagree=True,
        abstain_delta=delta,
        disagree_delta=0.0,
    )
    assert both is not None
    small_row = next(row for row in both["needs_human_review"] if row["shown_at"] == small.shown_at.isoformat())
    assert small_row["review_reason"] == "disagreement"
    assert any(row["shown_at"] == large.shown_at.isoformat() for row in both["queue_disagreements"])


def test_disagreement_with_the_encoder_flags_alerts_and_does_not_page():
    class Encoder:
        def embed(self, template: str) -> tuple[float, float]:
            if "disk" in template:
                return (-1.0, -1.0)
            return (1.0, 1.0)

    first, second = _alert("api", 1, "disk full"), _alert("api", 2, "heartbeat")
    items = [first, second]
    ranker = _pointwise("kind_alert", 5.0, 0.1)
    # K=4 leaves one alert slot (a quarter of K). K=1 leaves none, so both alerts would be dropped.
    config = Config(budget_k=4)
    shown = ranker.select(items, None, config)
    assert [item.shown_at for item in shown] == [first.shown_at]
    audit = build_ranker_audit(
        ranker,
        items,
        shown,
        None,
        config,
        explain=False,
        abstain=False,
        disagree=True,
        encoder=Encoder(),
        disagree_delta=0.0,
    )
    assert audit is not None
    assert audit["partner"] == "contrastive+baseline"
    assert audit["pages"] is False
    assert any(row["shown_at"] == first.shown_at.isoformat() for row in audit["queue_disagreements"])
    assert any(row["shown_at"] == second.shown_at.isoformat() for row in audit["needs_human_review"])
    assert all(row["pages"] is False for row in audit["needs_human_review"])
    paged = page_items([], [], spikes=[])
    would = would_have_paged([], [], spikes=[])
    assert paged == []
    assert would["count"] == 0


def test_hypothetical_coverage_point_and_an_empty_contrast():
    point = hypothetical_point(
        np.array([0.1, 0.48, 0.9]),
        np.array([False, False, True]),
        np.array([False, False, False]),
        np.array([False, True, True]),
        0.5,
        0.05,
    )
    assert point["candidates"] == 3
    assert point["review"] == 1
    assert point["keeps"] == 1 and point["drops"] == 1
    assert point["coverage"] == pytest.approx(2 / 3)
    assert point["accuracy"] == pytest.approx(1.0)
    assert point["precision"] == pytest.approx(1.0)
    chosen = choose_operating_points([{"delta": 0.0, "coverage": 1.0, "accuracy": 0.5, "precision": 0.5}])
    assert chosen[0]["target_coverage"] == 0.5
    assert chosen[0]["target_reached"] is False
    mask = np.array([True, True])
    report = bootstrap_rate_difference(np.array([1.0, 0.0]), mask, n_draw=20, seed=1)
    assert report["status"] == "no-contrast"
    assert report["difference"] is None
    same = disagreement_mask(
        np.array([0.2, 0.9]),
        np.array([True, True]),
        np.array([False, False]),
        np.array([True, True]),
        0.5,
        0.0,
    )
    assert same.tolist() == [False, False]
    review = live_review_mask(
        np.array([0.2, 0.9]),
        np.array([False, True]),
        np.array([False, False]),
        0.5,
        0.1,
    )
    assert review.tolist() == [False, False]


def test_a_bad_abstain_delta_falls_back_and_the_note_is_not_a_page(monkeypatch):
    monkeypatch.setenv("RANKER_ABSTAIN", "on")
    monkeypatch.setenv("RANKER_ABSTAIN_DELTA", "nope")
    items = [_jump("api", 1, 2.0), _jump("db", 4, 40.0)]
    ranker = _pointwise("log_rise", 1.0, 0.2)
    audit = build_ranker_audit(ranker, items, ranker.select(items, None, Config(budget_k=5)), None, Config(budget_k=5))
    assert audit is not None
    assert audit["pages"] is False
    assert audit["abstain_delta"] >= 0
    rows = [{"shown_at": "2026-03-01T01:00:00+00:00", "kind": "jump", "service": "api", "detail": "rise"}]
    _attach_ranker_notes(
        rows,
        {
            "queue_reasons": [
                {
                    "shown_at": "2026-03-01T01:00:00+00:00",
                    "kind": "jump",
                    "service": "api",
                    "reasons": [{"feature": "log_rise", "contribution": 0.25}],
                }
            ]
        },
    )
    assert rows[0]["ranker_pages"] is False
    assert "log_rise" in rows[0]["ranker_note"]
    untouched = [{"shown_at": "2026-03-01T01:00:00+00:00", "kind": "jump", "service": "api"}]
    _attach_ranker_notes(untouched, {"queue_reasons": []})
    assert "ranker_note" not in untouched[0]


def test_job_meta_hides_the_audit_until_one_exists():
    stamps = pd.date_range(T0, periods=30, freq="1min")
    values = np.full(len(stamps), 2e9)
    values[10:16] += 1.2e9
    metrics = pd.DataFrame({"timestamp": stamps, "value": values, "service": "api", "series_id": "api"})
    result = score_alerts([], metrics=metrics, config=Config(leak_window_minutes=None), use_saved_detector=False)
    inputs = JobInputs(alerts=[], metrics=metrics, config=Config())
    meta = _job_meta(result, inputs, None, 0, {}, {"count": 0, "items": []}, {})
    assert "ranker_audit" not in meta
    assert result.optional_models == {"metric_model": "off", "learned_ranker": "off"}
    result.optional_models = {
        "metric_model": "off",
        "learned_ranker": "applied",
        "ranker_audit": {"pages": False, "review_count": 2, "needs_human_review": []},
    }
    meta = _job_meta(result, inputs, None, 0, {}, {"count": 0, "items": []}, {})
    assert meta["ranker_audit"]["pages"] is False
    assert meta["ranker_audit"]["review_count"] == 2
    assert meta["would_have_paged"]["items"] == []


def test_the_queue_page_has_a_review_bucket_that_says_it_is_not_paged():
    text = (ROOT / "product" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="ranker-audit"' in text
    assert "not paged" in text
    assert "renderRankerAudit" in text


def test_audit_code_does_not_import_pickle_or_the_pager():
    tree = ast.parse((ROOT / "src" / "ranker_audit.py").read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module.split(".", 1)[0])
    assert "pickle" not in modules
    assert "paging" not in modules
    assert "pagerduty" not in modules
    for name in ("product/paging.py", "product/pagerduty.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "needs_human_review" not in text
        assert "ranker_audit" not in text
