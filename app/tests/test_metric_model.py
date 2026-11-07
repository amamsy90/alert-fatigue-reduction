"""Optional metric model (FR-25). Torch tests skip when torch is not installed."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from product.pipeline import score_alerts
from src.config import Config
from src.entities import Fault
from src.metric_model import (
    channel_frame,
    fit_norm,
    load_metric_model,
    metric_model_enabled,
    proposals_from_scores,
    save_metric_model,
    score_matrix,
    training_channels,
)
from src.optional_models import finish_queue
from src.queue import QueueItem, build_queue
from src.weight_io import save_bytes

T0 = pd.Timestamp("2026-01-05", tz="UTC")


def _metrics() -> pd.DataFrame:
    stamps = pd.date_range(T0, periods=80, freq="1min")
    rows = []
    for service, base in (("api", 2.0e9), ("db", 1.5e9)):
        memory = np.full(len(stamps), base)
        cpu = np.full(len(stamps), 0.2)
        memory[40:50] += 1.5e9
        cpu[40:50] = 0.9
        for metric, values in (("memory", memory), ("cpu", cpu)):
            rows.append(
                pd.DataFrame(
                    {
                        "timestamp": stamps,
                        "value": values,
                        "service": service,
                        "metric": metric,
                        "series_id": f"{service}:{metric}",
                    }
                )
            )
    return pd.concat(rows, ignore_index=True)


def test_metric_model_is_off_unless_the_flag_is_set(monkeypatch):
    monkeypatch.delenv("METRIC_MODEL", raising=False)
    assert metric_model_enabled() is False
    for value in ("1", "on", "true", "yes"):
        monkeypatch.setenv("METRIC_MODEL", value)
        assert metric_model_enabled() is True
    monkeypatch.setenv("METRIC_MODEL", "off")
    assert metric_model_enabled() is False


def test_windows_and_norm_ignore_later_points():
    frame = _metrics()
    split = T0 + pd.Timedelta(minutes=30)
    full = channel_frame(frame, freq="1min")
    cut = channel_frame(frame, freq="1min", until=split)
    for service in cut:
        full_index, full_values, _names = full[service]
        cut_index, cut_values, _cut_names = cut[service]
        mask = full_index <= split
        assert list(cut_index) == list(full_index[mask])
        assert np.allclose(cut_values, full_values[mask])
    later = frame.copy()
    later.loc[later["timestamp"] >= split, "value"] = 9e12
    channels = training_channels(frame, "1min", split)
    assert channels == ["cpu", "memory"]
    mean_before, _std = fit_norm(channel_frame(frame, freq="1min", until=split), channels, split)
    mean_after, _std_after = fit_norm(channel_frame(later, freq="1min", until=split), channels, split)
    assert np.allclose(mean_before, mean_after)
    # A point after the split must not move the memory mean up to billions more.
    assert mean_before[channels.index("memory")] < 3e9

    index, values, names = full["api"]
    grid = values  # already cpu, memory in sorted order
    seen = {}

    def forecast(batch: np.ndarray) -> np.ndarray:
        # Predict the newest context step. The target itself is not an input.
        return batch[:, :, -1]

    first = score_matrix(grid, index, forecast, np.zeros(2), np.ones(2), window=4, stride=1, end=split)
    second = score_matrix(grid, index, forecast, np.zeros(2), np.ones(2), window=4, stride=1)
    for stamp, score in first:
        seen[stamp] = score
        assert stamp < split
    for stamp, score in second:
        if stamp in seen:
            assert score == seen[stamp]


def test_gaia_style_series_id_is_one_memory_channel():
    stamps = pd.date_range(T0, periods=5, freq="1min")
    frame = pd.DataFrame(
        {"timestamp": stamps, "value": np.arange(5), "service": "dbservice1", "series_id": "dbservice1"}
    )
    _index, _values, names = channel_frame(frame, freq="1min")["dbservice1"]
    assert names == ["memory"]


def test_hash_mismatch_and_pickle_do_not_load(tmp_path):
    meta = tmp_path / "model.json"
    save_bytes(meta, b'{"format":"oncall-metric-tcn-v1"}')
    meta.with_name("model.json.sha256").write_text("0" * 64 + "\n", encoding="ascii")
    loaded, status = load_metric_model(tmp_path)
    assert loaded is None and status == "hash-mismatch"

    blob = b"\x80\x04\x95\x00"
    digest_path = tmp_path / "pickled.json"
    # Write the sidecar by hand so the hash matches and the refusal is the format check.
    import hashlib

    digest_path.write_bytes(blob)
    digest_path.with_name("pickled.json.sha256").write_text(hashlib.sha256(blob).hexdigest() + "\n", encoding="ascii")
    from src.weight_io import load_bytes

    body, why = load_bytes(digest_path)
    assert body is None and why == "bad-format"


def test_missing_weights_leave_the_queue_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("METRIC_MODEL", "on")
    monkeypatch.setenv("METRIC_MODEL_DIR", str(tmp_path / "absent"))
    monkeypatch.delenv("LEARNED_RANKER", raising=False)
    stamps = pd.date_range(T0, periods=4, freq="1min")
    spike_time = stamps[1]
    from src.memory_spikes import Spike

    spike = Spike("api", "api", spike_time, spike_time, confirm=spike_time, rise_at_confirm=1.0, kind="jump")
    items = [QueueItem(spike_time, "jump", "api", spike)]
    config = Config(budget_k=5)
    shown, status = finish_queue(items, None, config)
    assert status["metric_model"] == "missing"
    base = build_queue(items, config)
    assert [(item.shown_at, item.kind) for item in shown] == [(item.shown_at, item.kind) for item in base]


def test_score_alerts_ignores_weights_when_flags_are_off(monkeypatch, tmp_path):
    monkeypatch.delenv("METRIC_MODEL", raising=False)
    monkeypatch.delenv("LEARNED_RANKER", raising=False)
    monkeypatch.setenv("METRIC_MODEL_DIR", str(tmp_path))
    (tmp_path / "model.json").write_text("not a model", encoding="utf-8")
    stamps = pd.date_range(T0, periods=30, freq="1min")
    values = np.full(len(stamps), 2e9)
    values[10:16] += 1.2e9
    metrics = pd.DataFrame({"timestamp": stamps, "value": values, "service": "api", "series_id": "api"})
    result = score_alerts([], metrics=metrics, config=Config(leak_window_minutes=None), use_saved_detector=False)
    assert result.optional_models == {"metric_model": "off", "learned_ranker": "off"}


def test_a_loaded_metric_model_adds_a_rule_item_and_a_bad_score_does_not(monkeypatch, tmp_path):
    """FR-25. Applying the model adds a candidate. A scoring failure keeps the jump queue."""
    stamp = T0 + pd.Timedelta(minutes=5)

    class Archive:
        threshold = 1.0
        freq = "1min"
        stride = 1

        def score(self, metrics):
            return {"api": [(stamp, 4.0)]}

    monkeypatch.setattr("src.optional_models.load_metric_model", lambda directory: (Archive(), "ok"))
    monkeypatch.setenv("METRIC_MODEL", "on")
    monkeypatch.setenv("METRIC_MODEL_DIR", str(tmp_path))
    monkeypatch.delenv("LEARNED_RANKER", raising=False)
    metrics = pd.DataFrame({"timestamp": [stamp], "value": [1.0], "service": ["api"]})
    shown, status = finish_queue([], metrics, Config(budget_k=5))
    assert status == {"metric_model": "applied", "learned_ranker": "off"}
    assert [item.kind for item in shown] == ["rule"]
    assert shown[0].shown_at == stamp

    class Boom(Archive):
        def score(self, metrics):
            raise RuntimeError("forecast failed")

    monkeypatch.setattr("src.optional_models.load_metric_model", lambda directory: (Boom(), "ok"))
    from src.memory_spikes import Spike

    jump_at = stamp
    spike = Spike("api", "api", jump_at, jump_at, confirm=jump_at, rise_at_confirm=1.0, kind="jump")
    items = [QueueItem(jump_at, "jump", "api", spike)]
    shown, status = finish_queue(items, metrics, Config(budget_k=5))
    assert status["metric_model"] == "failed"
    assert [item.kind for item in shown] == ["jump"]


def test_proposals_are_rule_items_and_do_not_page():
    stamp = T0 + pd.Timedelta(minutes=5)
    scores = {"api": [(stamp, 3.0), (stamp + pd.Timedelta(minutes=1), 0.1)]}
    proposals = proposals_from_scores(scores, 1.0, [], freq="1min", stride=1)
    assert len(proposals) == 1
    assert proposals[0].kind == "rule"
    assert proposals[0].shown_at == stamp
    root = Path(__file__).parents[1]
    for name in ("src/metric_model.py", "src/optional_models.py", "src/ranker.py",
                 "experiments/model_ablation/model_eval.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [(node.module or "").split(".", 1)[0]]
            else:
                continue
            assert "pickle" not in modules
            assert "pagerduty" not in modules
            assert "paging" not in modules


@pytest.mark.ml
def test_tiny_cpu_tcn_trains_and_round_trips(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("safetensors")
    from src.metric_model import MetricArchive, fit_metric_model

    stamps = pd.date_range(T0, periods=200, freq="1min")
    rows = []
    for service in ("api", "db"):
        memory = np.full(len(stamps), 1.0)
        cpu = np.full(len(stamps), 0.2)
        # A spike in the training week, wide enough that normal windows remain.
        if service == "api":
            memory[80:90] = 8.0
            cpu[80:90] = 0.95
        for metric, values in (("memory", memory), ("cpu", cpu), ("latency", np.full(len(stamps), 10.0))):
            rows.append(
                pd.DataFrame(
                    {
                        "timestamp": stamps,
                        "value": values,
                        "service": service,
                        "metric": metric,
                    }
                )
            )
    frame = pd.concat(rows, ignore_index=True)
    split = T0 + pd.Timedelta(minutes=120)
    faults = [Fault("f1", stamps[80], stamps[90], "api", "spike")]
    archive, status = fit_metric_model(
        frame,
        faults,
        split,
        freq="1min",
        window=8,
        stride=4,
        hidden=4,
        kernel=3,
        dilations=(1, 2),
        epochs=3,
        seed=0,
        device="cpu",
        max_windows=64,
    )
    assert status == "trained" and archive is not None
    assert archive.device == "cpu"
    assert archive.channels == ["cpu", "latency", "memory"]
    digest = save_metric_model(archive, tmp_path)
    loaded, why = load_metric_model(tmp_path, expected_sha=digest)
    assert why == "ok" and loaded is not None
    assert loaded.weights_sha256 == digest
    quiet = archive.score(frame, end=stamps[40])
    hot = archive.score(frame, start=stamps[80], end=stamps[95])
    quiet_max = max(score for series in quiet.values() for _stamp, score in series)
    hot_max = max(score for series in hot.values() for _stamp, score in series)
    assert hot_max > quiet_max
    meta = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert meta["pages"] is False
    assert meta["objective"] == "one-step-forecast-mse"
    assert isinstance(archive, MetricArchive)
