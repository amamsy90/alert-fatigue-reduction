"""Synthetic ablation of the two optional models. GAIA numbers are not invented."""
from __future__ import annotations

import pandas as pd

from experiments.model_ablation.model_eval import EvalSpec, evaluate_models, published_bounds, walk_forward_models
from src.config import Config
from src.entities import Alert, Fault
from src.sample_data import generate_multivariate, load_multivariate

SPEC = EvalSpec(
    freq="5min",
    window=8,
    stride=4,
    hidden=4,
    dilations=(1,),
    epochs=1,
    backend="pointwise",
    device="cpu",
    max_windows=32,
    budget_ks=(5, 10, 20, 50),
    n_draw=200,
)
FAST = Config(leak_window_minutes=None, memory_gap_minutes=10)


def test_synthetic_ablation_reports_catch_at_each_k_and_leaves_gaia_pending(tmp_path):
    generate_multivariate(tmp_path, seed=42, days=14, freq="5min")
    metrics, alerts, faults = load_multivariate(tmp_path)
    report = evaluate_models(metrics, alerts, faults, config=FAST, spec=SPEC, dataset="synthetic")
    assert report["gaia"].startswith("pending")
    assert report["flags_changed"] is False
    assert report["metric_model"]["pages"] is False
    assert set(report["by_k"]) == {"5", "10", "20", "50"}
    for block in report["by_k"].values():
        assert set(block) == {"baseline", "metric_model", "ranker", "both"}
        base = block["baseline"]
        assert base["decision"] == "baseline"
        assert base["faults"] > 0
        assert "false_items_per_day" in base
        for name in ("metric_model", "ranker", "both"):
            row = block[name]
            assert row["decision"] in ("keep", "drop", "skipped")
            delta = row["per_fault_catch_delta"]
            if row["decision"] == "skipped":
                assert delta is None
            else:
                assert row["decision"] == ("keep" if delta["low"] > 0 else "drop")
                assert "false_items_per_day" in row


def test_walk_forward_refits_on_the_past_only(tmp_path):
    generate_multivariate(tmp_path, seed=1, days=21, freq="5min")
    metrics, alerts, faults = load_multivariate(tmp_path)
    report = walk_forward_models(
        metrics,
        alerts,
        faults,
        config=Config(budget_k=20, leak_window_minutes=None, memory_gap_minutes=10),
        spec=EvalSpec(
            freq="5min",
            window=8,
            stride=4,
            hidden=4,
            dilations=(1,),
            epochs=1,
            backend="pointwise",
            max_windows=24,
            n_draw=100,
        ),
        dataset="synthetic",
    )
    assert report["gaia"].startswith("pending")
    assert report["flags_changed"] is False
    assert len(report["folds"]) >= 2
    previous = None
    for fold in report["folds"]:
        assert fold["train_end"] < fold["test_end"]
        if previous is not None:
            assert fold["train_end"] == previous
        previous = fold["test_end"]
        assert "20" in fold["by_k"]
    pooled = report["pooled"]
    total = sum(fold["test_faults"] for fold in report["folds"])
    for name in ("metric_model", "ranker", "both"):
        row = pooled[name]
        assert row["decision"] in ("keep", "drop", "skipped")
        delta = row["per_fault_catch_delta"]
        if row["decision"] == "skipped":
            assert delta is None
        else:
            assert delta["n"] == total
            assert row["decision"] == ("keep" if delta["low"] > 0 else "drop")


def test_the_scoring_window_ends_at_the_last_alert_not_the_last_metric():
    """The published queue stops one second after the last alert.

    A metric series that ends first must not drop a later fault. The split
    starts at the first alert, which is what load_gaia uses.
    """
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    first_alert = t0 + pd.Timedelta(hours=1)
    metric_end = t0 + pd.Timedelta(days=10)
    last_alert = t0 + pd.Timedelta(days=12, hours=3)
    stamps = pd.date_range(t0, metric_end, freq="1h", inclusive="left")
    metrics = pd.DataFrame(
        {
            "timestamp": stamps,
            "value": [1.0] * len(stamps),
            "service": "api",
            "series_id": "api",
        }
    )
    alerts = [
        Alert("a0", first_alert, "api", "log", "info", "start"),
        Alert("a1", last_alert, "api", "log", "error", "late"),
    ]
    faults = [
        Fault("early", t0 + pd.Timedelta(days=8), t0 + pd.Timedelta(days=8, hours=1), "api"),
        Fault("late", metric_end + pd.Timedelta(hours=2), metric_end + pd.Timedelta(hours=3), "api"),
        Fault("after", last_alert + pd.Timedelta(hours=1), last_alert + pd.Timedelta(hours=2), "api"),
    ]
    origin, stop, how = published_bounds(metrics, alerts)
    assert how == "last alert"
    assert origin == first_alert
    assert stop == last_alert + pd.Timedelta(seconds=1)
    report = evaluate_models(
        metrics,
        alerts,
        faults,
        config=Config(split_day=7, leak_window_minutes=None, memory_gap_minutes=10),
        spec=EvalSpec(
            freq="1h",
            window=4,
            stride=4,
            hidden=4,
            dilations=(1,),
            epochs=1,
            backend="pointwise",
            max_windows=8,
            budget_ks=(5,),
            n_draw=50,
        ),
        dataset="synthetic",
    )
    assert report["window_end"] == "last alert"
    assert report["train_end"] == (first_alert + pd.Timedelta(days=7)).isoformat()
    assert report["test_end"] == stop.isoformat()
    assert report["test_fault_ids"] == ["early", "late"]
    assert report["test_faults"] == 2
