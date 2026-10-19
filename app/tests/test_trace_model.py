"""Optional trace root-cause model. Torch tests skip when torch is not installed."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from product.paging import page_job
from src.config import Config
from src.entities import Action, Alert, Decision, Fault
from src.trace_graph import baseline_service, build_graph, normalize_spans, root_cause_hit
from src.trace_model import load_trace_model, trace_model_enabled

T0 = pd.Timestamp("2026-01-05", tz="UTC")
FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_traces.csv"


def test_trace_model_is_off_unless_the_flag_is_set(monkeypatch):
    monkeypatch.delenv("TRACE_MODEL", raising=False)
    assert trace_model_enabled() is False
    for value in ("1", "on", "true", "yes"):
        monkeypatch.setenv("TRACE_MODEL", value)
        assert trace_model_enabled() is True


def test_a_clock_column_is_not_used_when_start_time_millis_is_present():
    """RCAEval's `time` is text such as 21:24. The epoch is startTimeMillis."""
    frame = pd.DataFrame(
        {
            "time": pd.Series(["21:24", "21:25", "21:26"], dtype="string"),
            "startTimeMillis": [1735689600000, 1735689900000, 1735692000000],
            "traceID": ["t1", "t2", "t3"],
            "spanID": ["s1", "s2", "s3"],
            "serviceName": ["web", "api", "db"],
            "duration": [30, 20, 15],
            "statusCode": [0, 0, 1],
        }
    )
    spans = normalize_spans(frame)
    assert spans["time"].iloc[0] == pd.Timestamp(1735689600, unit="s", tz="UTC")
    assert spans["time"].iloc[1] == pd.Timestamp(1735689900, unit="s", tz="UTC")
    assert int(spans.loc[spans["span_id"] == "s3", "error"].iloc[0]) == 1


def test_a_clock_column_alone_is_refused():
    frame = pd.DataFrame(
        {
            "time": pd.Series(["21:24", "21:25"], dtype="string"),
            "serviceName": ["web", "db"],
            "duration": [1, 2],
        }
    )
    with pytest.raises(ValueError, match="21:24"):
        normalize_spans(frame)


def test_rcaeval_eval_names_the_cases_fetch_when_cases_are_missing(tmp_path):
    from run_trace_eval import cases_missing_message

    message = cases_missing_message(tmp_path)
    assert message is not None
    assert "fetch_rcaeval_re23.py" in message
    assert "cases.parquet" in message
    (tmp_path / "cases.parquet").write_bytes(b"")
    assert cases_missing_message(tmp_path) is None


def test_rcaeval_column_names_and_a_later_span_do_not_change_the_window():
    frame = normalize_spans(pd.read_csv(FIXTURE))
    assert list(frame["service"]) == ["web", "api", "db", "web", "api", "db", "web", "api", "db"]
    assert int(frame.loc[frame["span_id"] == "s6", "error"].iloc[0]) == 1
    start = pd.Timestamp(1735689700, unit="s", tz="UTC")
    end = pd.Timestamp(1735690000, unit="s", tz="UTC")
    window = build_graph(frame, start, end)
    trimmed = build_graph(frame[frame["time"] < end], start, end)
    assert window.services == trimmed.services
    assert window.node_x.tolist() == trimmed.node_x.tolist()
    # The hot db span is inside the window. The quiet span after `end` is not.
    assert "db" in window.services
    assert window.error_jump["db"] > 0
    assert baseline_service(window) == "db"
    assert root_cause_hit("db", "db") is True
    assert root_cause_hit("api", "db") is False


def test_baseline_names_the_caller_when_its_latency_jump_is_larger():
    """The non-learned rule is the biggest jump, not the root-cause label."""
    start = T0
    mid = T0 + pd.Timedelta(minutes=5)
    end = T0 + pd.Timedelta(minutes=10)
    rows = []
    for stamp, api, db, error in (
        (start - pd.Timedelta(minutes=5), 20.0, 15.0, 0),
        (mid, 160.0, 18.0, 1),
    ):
        rows.extend(
            [
                {"time": stamp, "trace_id": "t", "span_id": f"{stamp}-w", "parent_span_id": "", "service": "web", "duration": 30.0, "error": 0},
                {"time": stamp, "trace_id": "t", "span_id": f"{stamp}-a", "parent_span_id": f"{stamp}-w", "service": "api", "duration": api, "error": 0},
                {"time": stamp, "trace_id": "t", "span_id": f"{stamp}-d", "parent_span_id": f"{stamp}-a", "service": "db", "duration": db, "error": error},
            ]
        )
    graph = build_graph(pd.DataFrame(rows), start, end)
    assert baseline_service(graph) == "api"
    fault = Fault("f", start, end, "db", "decoy")
    assert root_cause_hit(baseline_service(graph), fault.component) is False


def test_pickle_and_a_bad_hash_do_not_load(tmp_path):
    blob = b"\x80\x04\x95\x00"
    path = tmp_path / "model.safetensors"
    path.write_bytes(blob)
    path.with_name("model.safetensors.sha256").write_text(hashlib.sha256(blob).hexdigest() + "\n", encoding="ascii")
    meta = tmp_path / "model.json"
    meta.write_bytes(b'{"format":"oncall-trace-gnn-v1","pages":false}')
    meta.with_name("model.json.sha256").write_text(hashlib.sha256(meta.read_bytes()).hexdigest() + "\n", encoding="ascii")
    loaded, status = load_trace_model(tmp_path)
    assert loaded is None and status == "bad-format"


def test_sock_shop_is_not_a_trace_download():
    import importlib.util

    path = Path(__file__).parents[2] / "tools" / "fetch_rcaeval_traces.py"
    spec = importlib.util.spec_from_file_location("fetch_rcaeval_traces", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folders = module.cases(("RE2", "RE3"))
    assert folders
    assert all(not folder.lower().startswith(("re2ss_", "re3ss_")) for folder in folders)
    assert any(folder.startswith("re2ob_") for folder in folders)
    assert any(folder.startswith("re2tt_") for folder in folders)


def test_the_flag_does_not_change_what_would_be_paged(monkeypatch):
    monkeypatch.setenv("TRACE_MODEL", "on")
    monkeypatch.setenv("TRACE_MODEL_DIR", "/tmp/does-not-exist-trace-model")
    stamp = T0
    alert = Alert("a1", stamp, "web", "oom", "error", "disk full")
    decision = Decision("a1", Action.ESCALATE, 0.9, "policy")
    page, whp = page_job("sample", True, [alert], [decision], [])
    assert page["skipped"] == "mode=sample never pages"
    assert whp["count"] == 1
    root = Path(__file__).parents[1] / "src" / "trace_model.py"
    tree = ast.parse(root.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".", 1)[0] not in {"paging", "pagerduty"}


def _sidecar(path: Path, blob: bytes) -> None:
    path.write_bytes(blob)
    path.with_name(path.name + ".sha256").write_text(hashlib.sha256(blob).hexdigest() + "\n", encoding="ascii")


def test_without_torch_the_eval_still_scores_the_baseline_and_the_queue(monkeypatch):
    """A missing torch install skips the model. It does not invent a catch rate."""
    from src.trace_eval import TraceSpec, _decision, _model_decision, evaluate_traces, synthetic_traces
    from src.trace_model import fit_trace_model, trace_model_directory, training_graphs

    monkeypatch.setattr("src.trace_model.torch_stack_available", lambda: False)
    assert trace_model_directory() is None
    spans, metrics, faults = synthetic_traces(seed=1, days=2)
    archive, status = fit_trace_model(spans, faults, spans["time"].min() + pd.Timedelta(days=7))
    assert archive is None and status == "torch-missing"
    boundary = spans["time"].min() + pd.Timedelta(days=7)
    graphs, labels = training_graphs(spans, faults, boundary)
    later = [fault for fault in faults if fault.start >= boundary]
    assert training_graphs(spans, later, boundary) == ([], [])
    assert graphs and all(label == graph.services.index("db") for graph, label in zip(graphs, labels, strict=True))
    early = Fault("early", T0, T0, "missing", "zero")
    absent = Fault("absent", T0 + pd.Timedelta(minutes=10), T0 + pd.Timedelta(minutes=30), "nope", "x")
    clipped, indexes = training_graphs(spans, [early, absent, faults[0]], T0 + pd.Timedelta(hours=1))
    assert clipped == [] and indexes == []
    report = evaluate_traces(
        spans,
        metrics,
        faults,
        alerts=[Alert("a-test", T0 + pd.Timedelta(days=8), "db", "oom", "error", "disk full")],
        config=Config(),
        spec=TraceSpec(epochs=1, seed=1, backend="plain"),
    )
    assert report["pages"] is False
    assert report["flags_changed"] is False
    assert report["trace_model"]["status"] == "torch-missing"
    assert report["headline"]["trace_model"]["decision"] == "skipped"
    assert report["gaia"].startswith("pending")
    assert report["rcaeval"].startswith("pending")
    assert set(report["by_k"]) == {"5", "10", "20", "50"}
    empty = evaluate_traces(pd.read_csv(FIXTURE), pd.DataFrame(), [], dataset="heldout")
    assert empty["trace_model"]["status"] == "torch-missing"
    assert "first half" in empty["split"]
    held = [Fault(f"f{i}", T0, T0 + pd.Timedelta(hours=1), "db", "mem") for i in range(4)]
    named = {fault.fault_id for fault in held}
    assert _model_decision(set(), set(), named, held, seed=1)["decision"] == "keep"
    assert _model_decision(named, named, set(), held, seed=1)["decision"] == "drop"
    assert _decision(set(), set(), [], 1)["decision"] == "skipped"


def test_load_refuses_a_bad_archive_before_any_weight_library(tmp_path, monkeypatch):
    loaded, status = load_trace_model(tmp_path)
    assert loaded is None and status == "missing"
    meta = tmp_path / "model.json"
    _sidecar(meta, b"not-json")
    _sidecar(tmp_path / "model.safetensors", b'{"weights":true}\n')
    loaded, status = load_trace_model(tmp_path)
    assert loaded is None and status == "bad-format"
    payload = {"format": "oncall-trace-gnn-v1", "pages": True, "hidden": 4, "weights_sha256": "ab" * 32}
    _sidecar(meta, json.dumps(payload).encode("utf-8"))
    loaded, status = load_trace_model(tmp_path)
    assert loaded is None and status == "bad-format"
    blob = (tmp_path / "model.safetensors").read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    payload = {"format": "oncall-trace-gnn-v1", "pages": False, "hidden": 4, "weights_sha256": digest}
    _sidecar(meta, json.dumps(payload).encode("utf-8"))
    monkeypatch.setenv("TRACE_MODEL_SHA256", "cd" * 32)
    monkeypatch.setenv("TRACE_MODEL_DIR", str(tmp_path))
    from src.trace_model import trace_model_directory

    assert trace_model_directory() == tmp_path
    loaded, status = load_trace_model(tmp_path)
    assert loaded is None and status == "hash-mismatch"


@pytest.mark.ml
def test_auto_uses_geometric_when_it_imports():
    pytest.importorskip("torch")
    pytest.importorskip("torch_geometric")
    from src.trace_gnn import resolve_gnn_backend

    backend, fell_back = resolve_gnn_backend("auto")
    assert backend == "pyg" and fell_back is False
    plain, plain_fell = resolve_gnn_backend("plain")
    assert plain == "plain" and plain_fell is False


@pytest.mark.ml
def test_tiny_cpu_gnn_trains_and_round_trips(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("safetensors")
    from src.trace_eval import TraceSpec, evaluate_traces, synthetic_traces
    from src.trace_model import fit_trace_model, save_trace_model

    spans, metrics, faults = synthetic_traces(seed=0)
    boundary = spans["time"].min() + pd.Timedelta(days=7)
    archive, status = fit_trace_model(
        spans, faults, boundary, hidden=8, epochs=8, seed=0, device="cpu", backend="plain"
    )
    assert status == "trained" and archive is not None
    assert archive.backend == "plain"
    assert archive.fell_back is False
    digest = save_trace_model(archive, tmp_path)
    loaded, why = load_trace_model(tmp_path, expected_sha=digest)
    assert why == "ok" and loaded is not None
    meta = (tmp_path / "model.json").read_text(encoding="utf-8")
    assert '"pages": false' in meta
    named = loaded.predict_service(spans, faults[-1].start, faults[-1].end)
    assert named in {"web", "api", "db"}
    report = evaluate_traces(
        spans,
        metrics,
        faults,
        config=Config(),
        spec=TraceSpec(epochs=4, seed=0, backend="plain"),
    )
    assert report["pages"] is False
    assert report["flags_changed"] is False
    assert report["gaia"].startswith("pending")
    assert report["rcaeval"].startswith("pending")
    assert report["headline_k"] == 20
    assert report["headline"]["trace_model"]["decision"] in {"keep", "drop"}
