"""Synthetic ablation of the two optional models. GAIA numbers are not invented."""
from __future__ import annotations

from src.config import Config
from src.model_eval import EvalSpec, evaluate_models, walk_forward_models
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
