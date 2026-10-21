"""Optional learned ranker (FR-26). XGBoost and torch paths skip when absent."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.accel import NODE_DTYPE
from src.config import Config
from src.entities import Alert, Fault
from src.memory_spikes import Spike
from src.optional_models import finish_queue
from src.queue import QueueItem, build_queue
from src.ranker import (
    FEATURES,
    LearnedRanker,
    candidate_frame,
    fit_ranker,
    learned_ranker_enabled,
    load_ranker,
    resolve_backend,
    save_ranker,
    select_by_score,
    tree_margins,
)

T0 = pd.Timestamp("2026-03-01 00:00:00", tz="UTC")


def _jump(service: str, hour: int, rise: float, kind: str = "jump") -> QueueItem:
    start = T0 + pd.Timedelta(hours=hour)
    end = start + pd.Timedelta(minutes=10)
    spike = Spike(
        f"{service}:{kind}",
        service,
        start,
        end,
        peak_rise=rise,
        confirm=start,
        rise_at_confirm=rise,
        level_at_confirm=1.0,
        kind=kind,
    )
    return QueueItem(start, kind, service, spike)


def _alert(service: str, hour: int, fault_id: str | None) -> QueueItem:
    stamp = T0 + pd.Timedelta(hours=hour)
    alert = Alert(f"a-{hour}", stamp, service, "log", "error", "disk full", fault_id=fault_id)
    return QueueItem(stamp, "alert", service, alert)


def test_a_loaded_ranker_picks_the_slot_and_waits_for_a_metric_score(monkeypatch, tmp_path):
    """FR-26. The ranker chooses among candidates. It does not invent a score it was trained with."""
    early = _jump("api", 1, 2.0)
    late = _jump("db", 2, 9.0)

    class Ranker:
        uses_metric_score = False

        def select(self, items, scores, config):
            return [items[0]]

    monkeypatch.setattr("src.optional_models.load_ranker", lambda path: (Ranker(), "ok"))
    monkeypatch.setenv("LEARNED_RANKER", "on")
    monkeypatch.setenv("LEARNED_RANKER_PATH", str(tmp_path / "ranker.json"))
    monkeypatch.delenv("METRIC_MODEL", raising=False)
    shown, status = finish_queue([early, late], None, Config(budget_k=5))
    assert status["learned_ranker"] == "applied"
    assert [item.payload for item in shown] == [early.payload]

    class NeedsScore(Ranker):
        uses_metric_score = True

    monkeypatch.setattr("src.optional_models.load_ranker", lambda path: (NeedsScore(), "ok"))
    shown, status = finish_queue([early, late], None, Config(budget_k=5))
    assert status["learned_ranker"] == "needs-metric-model"
    assert [item.payload for item in shown] == [early.payload, late.payload]


def test_ranker_is_off_unless_the_flag_is_set(monkeypatch):
    monkeypatch.delenv("LEARNED_RANKER", raising=False)
    assert learned_ranker_enabled() is False
    monkeypatch.setenv("LEARNED_RANKER", "on")
    assert learned_ranker_enabled() is True


def test_ranker_features_have_no_fault_id():
    items = [_jump("api", 1, 10.0), _alert("api", 2, "SECRET-FAULT")]
    frame = candidate_frame(items, {"api": [(T0, 0.4)]}, Config())
    assert list(frame.columns) == list(FEATURES)
    assert "fault_id" not in frame.columns
    assert "SECRET-FAULT" not in frame.to_csv()
    # A score at midnight is known by the 01:00 jump. A score after that jump is not.
    assert frame.loc[0, "metric_score"] == 0.4
    after = candidate_frame(items, {"api": [(T0 + pd.Timedelta(hours=1, minutes=1), 1.5)]}, Config())
    assert after.loc[0, "metric_score"] == 0.0
    assert after.loc[1, "metric_score"] == 1.5


def test_a_low_threshold_matches_first_come_first_served():
    items = [_jump("api", 1, 2.0), _jump("db", 2, 9.0), _alert("api", 3, None)]
    config = Config(budget_k=2, alert_share=0.5, gap_share=0.25)
    scores = [0.2, 0.8, 0.1]
    ranked = select_by_score(items, scores, config, threshold=-1.0)
    base = build_queue(
        [QueueItem(item.shown_at, item.kind, item.service, item.payload) for item in items],
        config,
    )
    assert [(item.shown_at, item.kind, item.service, item.attached) for item in ranked] == [
        (item.shown_at, item.kind, item.service, item.attached) for item in base
    ]


def test_low_scores_do_not_fill_the_budget():
    """K=1. A weak early jump must not take the slot a later fault jump needs."""
    noise = _jump("api", 1, 1.0)
    real = _jump("api", 15, 20.0)
    config = Config(budget_k=1)
    first = build_queue([noise, real], config)
    assert [item.shown_at for item in first] == [noise.shown_at]
    chosen = select_by_score([noise, real], [0.1, 0.9], config, threshold=0.5)
    assert [item.shown_at for item in chosen] == [real.shown_at]


def test_a_day_that_fits_in_k_keeps_low_scores():
    """K=50-style: two candidates and a large budget show both, even below the threshold."""
    early = _jump("api", 1, 1.0)
    late = _jump("db", 4, 2.0)
    config = Config(budget_k=5)
    scores = [0.1, 0.2]
    ranked = select_by_score([early, late], scores, config, threshold=0.9)
    base = build_queue(
        [QueueItem(item.shown_at, item.kind, item.service, item.payload) for item in (early, late)],
        config,
    )
    assert [(item.shown_at, item.service) for item in ranked] == [(item.shown_at, item.service) for item in base]
    assert len(ranked) == 2


def test_a_later_fault_does_not_change_the_fit():
    items = [_jump("api", 1, 1.0), _jump("api", 4, 30.0), _jump("db", 2, 1.0), _jump("db", 6, 30.0)]
    faults = [
        Fault("early", T0 + pd.Timedelta(hours=4), T0 + pd.Timedelta(hours=5), "api"),
        Fault("other", T0 + pd.Timedelta(hours=6), T0 + pd.Timedelta(hours=7), "db"),
    ]
    boundary = T0 + pd.Timedelta(hours=12)
    future = Fault("later", T0 + pd.Timedelta(days=2), T0 + pd.Timedelta(days=2, hours=1), "api")
    # The future fault overlaps nothing in the training items. A second future fault
    # overlaps a training spike's end only if we failed to drop faults that start
    # after the split. The spike at hour 4 ends at hour 4:10, so a fault at hour 5
    # on the next day does not overlap. Use a fault that starts after the split
    # but would overlap the hour-4 spike if the filter were missing.
    overlapping = Fault("leak", T0 + pd.Timedelta(hours=4, minutes=5), T0 + pd.Timedelta(hours=5), "api")
    overlapping_later = Fault(
        "leak-later",
        boundary + pd.Timedelta(minutes=1),
        boundary + pd.Timedelta(hours=2),
        "api",
    )
    # Move the hour-4 spike so its end crosses the boundary while shown_at is before it.
    crossing = _jump("api", 11, 30.0)
    crossing.payload.end = boundary + pd.Timedelta(hours=1)
    config = Config(budget_k=5)
    first, status = fit_ranker(
        items + [crossing], faults + [overlapping], config, None, train_end=boundary, backend="pointwise", seed=1
    )
    second, _status = fit_ranker(
        items + [crossing],
        faults + [overlapping, overlapping_later, future],
        config,
        None,
        train_end=boundary,
        backend="pointwise",
        seed=1,
    )
    assert status == "trained" and first is not None and second is not None
    assert np.allclose(first.coef, second.coef)
    assert first.intercept == second.intercept
    frame = candidate_frame(items, None, config)
    assert "fault_id" not in frame.columns


def test_ranker_hash_mismatch_keeps_the_file_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("LEARNED_RANKER_SHA256", raising=False)
    path = tmp_path / "ranker.json"
    path.write_text("{}", encoding="utf-8")
    path.with_name("ranker.json.sha256").write_text("ab" * 32 + "\n", encoding="ascii")
    loaded, status = load_ranker(path)
    assert loaded is None and status == "hash-mismatch"


def test_pointwise_ranker_round_trips_and_prefers_the_fault(tmp_path):
    items = []
    faults = []
    for day in range(4):
        base = T0 + pd.Timedelta(days=day)
        for hour, rise, fault in ((1, 1.0, False), (3, 1.2, False), (8, 40.0, True), (9, 1.1, False)):
            start = base + pd.Timedelta(hours=hour)
            spike = Spike("s", "api", start, start + pd.Timedelta(minutes=5), confirm=start,
                          rise_at_confirm=rise, level_at_confirm=1.0, kind="jump")
            items.append(QueueItem(start, "jump", "api", spike))
            if fault:
                faults.append(Fault(f"f{day}", start, start + pd.Timedelta(minutes=20), "api"))
    boundary = T0 + pd.Timedelta(days=3)
    ranker, status = fit_ranker(
        items, faults, Config(budget_k=1), None, train_end=boundary, backend="pointwise", seed=0
    )
    assert status == "trained" and ranker is not None
    assert ranker.backend == "pointwise" and ranker.device == "cpu"
    train_real = [item for item in items if item.shown_at < boundary and item.shown_at.hour == 8]
    train_noise = [item for item in items if item.shown_at < boundary and item.shown_at.hour == 1]
    real_score = ranker.score_frame(candidate_frame(train_real, None, Config()))[0]
    noise_score = ranker.score_frame(candidate_frame(train_noise, None, Config()))[0]
    assert real_score > noise_score
    digest = save_ranker(ranker, tmp_path / "ranker.json")
    loaded, why = load_ranker(tmp_path / "ranker.json", expected_sha=digest)
    assert why == "ok" and loaded is not None
    again = loaded.score_frame(candidate_frame(train_real, None, Config()))[0]
    assert again == pytest.approx(real_score)
    meta = (tmp_path / "ranker.json").read_text(encoding="utf-8")
    assert '"pages": false' in meta or '"pages":false' in meta


def test_tree_ranker_margin_round_trips(tmp_path):
    tree = np.zeros(1, dtype=NODE_DTYPE)
    tree[0]["feature"] = -1
    tree[0]["leaf"] = 1.25
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
    frame = candidate_frame([_jump("api", 1, 2.0)], None, Config())
    assert tree_margins(ranker.trees, ranker.base_score, frame.to_numpy())[0] == pytest.approx(1.35)
    save_ranker(ranker, tmp_path / "ranker.json")
    loaded, status = load_ranker(tmp_path / "ranker.json")
    assert status == "ok" and loaded is not None
    assert loaded.score_frame(frame)[0] == pytest.approx(1.35)


def test_auto_is_pointwise_without_optional_libraries(monkeypatch):
    monkeypatch.setattr("src.ranker.xgboost_importable", lambda: False)
    monkeypatch.setattr("src.ranker.torch_ranker_importable", lambda: False)
    assert resolve_backend("auto") == "pointwise"


@pytest.mark.ml
def test_tiny_cpu_torch_ranker_round_trips(tmp_path):
    pytest.importorskip("torch")
    items = []
    faults = []
    for day in range(3):
        base = T0 + pd.Timedelta(days=day)
        for hour, rise, fault in ((2, 1.0, False), (4, 1.0, False), (10, 50.0, True)):
            start = base + pd.Timedelta(hours=hour)
            spike = Spike("s", "db", start, start + pd.Timedelta(minutes=5), confirm=start,
                          rise_at_confirm=rise, level_at_confirm=1.0, kind="jump")
            items.append(QueueItem(start, "jump", "db", spike))
            if fault:
                faults.append(Fault(f"d{day}", start, start + pd.Timedelta(minutes=15), "db"))
    ranker, status = fit_ranker(
        items,
        faults,
        Config(budget_k=2),
        None,
        train_end=T0 + pd.Timedelta(days=4),
        backend="torch",
        device="cpu",
        seed=0,
    )
    assert status == "trained" and ranker is not None
    assert ranker.backend == "torch"
    save_ranker(ranker, tmp_path / "ranker.json")
    loaded, why = load_ranker(tmp_path / "ranker.json")
    assert why == "ok" and loaded is not None
    scores = loaded.score_frame(candidate_frame(items[:3], None, Config()))
    assert np.isfinite(scores).all()


@pytest.mark.gpu
def test_xgboost_ranker_converts_without_pickle(tmp_path):
    pytest.importorskip("xgboost")
    items = []
    faults = []
    for day in range(3):
        base = T0 + pd.Timedelta(days=day)
        for hour, rise, fault in ((2, 1.0, False), (6, 1.0, False), (12, 40.0, True)):
            start = base + pd.Timedelta(hours=hour)
            spike = Spike("s", "api", start, start + pd.Timedelta(minutes=5), confirm=start,
                          rise_at_confirm=rise, level_at_confirm=1.0, kind="jump")
            items.append(QueueItem(start, "jump", "api", spike))
            if fault:
                faults.append(Fault(f"x{day}", start, start + pd.Timedelta(minutes=10), "api"))
    ranker, status = fit_ranker(
        items,
        faults,
        Config(budget_k=2),
        None,
        train_end=T0 + pd.Timedelta(days=4),
        backend="xgboost",
        device="cpu",
        seed=0,
    )
    assert status == "trained" and ranker is not None
    assert ranker.backend == "xgboost"
    assert ranker.device == "cpu"
    save_ranker(ranker, tmp_path / "ranker.json")
    text = (tmp_path / "ranker.json").read_text(encoding="utf-8")
    assert "oncall-ranker-v1" in text
    loaded, why = load_ranker(tmp_path / "ranker.json")
    assert why == "ok" and loaded is not None
    assert np.allclose(
        loaded.score_frame(candidate_frame(items, None, Config())),
        ranker.score_frame(candidate_frame(items, None, Config())),
    )
