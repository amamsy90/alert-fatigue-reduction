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
from src.trace_graph import _build_graph_loops, baseline_service, build_graph, normalize_spans, root_cause_hit
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


def _same_graph(left, right) -> None:
    assert left.services == right.services
    assert left.node_x.shape == right.node_x.shape
    assert left.edge_index.shape == right.edge_index.shape
    assert left.edge_attr.shape == right.edge_attr.shape
    assert left.node_x.tolist() == right.node_x.tolist()
    assert left.edge_index.tolist() == right.edge_index.tolist()
    assert left.edge_attr.tolist() == right.edge_attr.tolist()
    assert left.error_jump == right.error_jump
    assert left.latency_jump == right.latency_jump


def test_vectorised_graph_matches_the_loop_version_on_a_small_fixture():
    """Groupby and the parent-span merge agree with the row loop."""
    frame = normalize_spans(pd.read_csv(FIXTURE))
    start = pd.Timestamp(1735689700, unit="s", tz="UTC")
    end = pd.Timestamp(1735690000, unit="s", tz="UTC")
    _same_graph(build_graph(frame, start, end), _build_graph_loops(frame, start, end))
    rows = []
    for stamp, api, db, error in (
        (T0 - pd.Timedelta(minutes=5), 20.0, 15.0, 0),
        (T0 + pd.Timedelta(minutes=5), 160.0, 18.0, 1),
    ):
        rows.extend(
            [
                {"time": stamp, "trace_id": "t", "span_id": f"{stamp}-w", "parent_span_id": "", "service": "web", "duration": 30.0, "error": 0},
                {"time": stamp, "trace_id": "t", "span_id": f"{stamp}-a", "parent_span_id": f"{stamp}-w", "service": "api", "duration": api, "error": 0},
                {"time": stamp, "trace_id": "t", "span_id": f"{stamp}-d", "parent_span_id": f"{stamp}-a", "service": "db", "duration": db, "error": error},
            ]
        )
    decoy = pd.DataFrame(rows)
    window = T0 + pd.Timedelta(minutes=10)
    _same_graph(build_graph(decoy, T0, window), _build_graph_loops(decoy, T0, window))


def test_a_prebuilt_case_graph_is_not_rebuilt(monkeypatch):
    """Fit and the baseline reuse the recording's graph instead of walking spans again."""
    frame = normalize_spans(pd.read_csv(FIXTURE))
    start = pd.Timestamp(1735689600, unit="s", tz="UTC")
    end = pd.Timestamp(1735690500, unit="s", tz="UTC")
    graph = build_graph(frame, start, end)
    fault = Fault("case-a", start, end, "db", "mem")

    def boom(*_args, **_kwargs):
        raise AssertionError("build_graph should not run when the case graph is already built")

    monkeypatch.setattr("src.trace_model.build_graph", boom)
    monkeypatch.setattr("src.trace_eval.build_graph", boom)
    from src.trace_eval import _baseline_ids
    from src.trace_model import training_graphs

    graphs, labels = training_graphs(pd.DataFrame(), [fault], end + pd.Timedelta(days=1), {"case-a": graph})
    assert labels == [graph.services.index("db")]
    assert graphs[0].services == graph.services
    caught = _baseline_ids(pd.DataFrame(), [fault], {"case-a": graph})
    assert ("case-a" in caught) is root_cause_hit(baseline_service(graph), "db")


def test_max_cases_keeps_the_first_recordings():
    from run_trace_eval import limit_cases, wanted_span_columns

    assert limit_cases(["a", "b", "c"], 2) == ["a", "b"]
    assert limit_cases(["a", "b"], 0) == ["a", "b"]
    assert limit_cases(["a"], None) == ["a"]
    columns = wanted_span_columns(["operationName", "startTimeMillis", "time", "tags", "serviceName", "spanID"])
    assert columns == ["startTimeMillis", "time", "serviceName", "spanID"]


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
    assert report["headline"]["trace_residual"]["decision"] in {"keep", "drop"}
    assert report["train_cases"] > 0
    assert report["test_cases"] == report["test_faults"]


def test_tiny_train_flags_fewer_than_twenty_cases():
    from src.trace_eval import suite_day, tiny_train

    assert tiny_train(7) is True
    assert tiny_train(19) is True
    assert tiny_train(20) is False
    assert suite_day(0, "RE2") == 0
    assert suite_day(6, "RE3") == 1006
    with pytest.raises(ValueError):
        suite_day(0, "RE1")


def test_combined_suites_do_not_share_a_series_timestamp():
    """RE2 and RE3 both start on 2024-01-01. The same series id must not repeat a timestamp."""
    from run_rcaeval_logs import Case, base_config
    from run_trace_eval import spread_case
    from src.memory_spikes import detect_jumps

    raw = Case("re2ob_cpu", 0, 1_700_000_000, 1_700_000_100, 1_700_001_000, "adservice", "cpu")
    other = Case("re3ob_cpu", 0, 1_700_000_000, 1_700_000_100, 1_700_001_000, "adservice", "cpu")
    assert raw.at(raw.start) == other.at(other.start)
    left, left_day = spread_case(raw, "RE2")
    right, right_day = spread_case(other, "RE3")
    assert left_day == 0 and right_day == 0
    assert right.at(right.start) - left.at(left.start) == pd.Timedelta(days=1000)
    shared = pd.DataFrame(
        {
            "timestamp": [raw.at(raw.start), other.at(other.start)],
            "value": [1.0, 2.0],
            "service": ["adservice", "adservice"],
            "series_id": ["adservice_cpu", "adservice_cpu"],
        }
    )
    assert shared.duplicated(["series_id", "timestamp"]).any()
    frame = pd.DataFrame(
        {
            "timestamp": [left.at(left.start), right.at(right.start)],
            "value": [1.0, 2.0],
            "service": ["adservice", "adservice"],
            "series_id": ["adservice_cpu", "adservice_cpu"],
        }
    )
    assert not frame.duplicated(["series_id", "timestamp"]).any()
    assert isinstance(detect_jumps(frame, base_config()), list)


def test_baseline_score_is_an_extra_column_and_a_separate_variant(monkeypatch):
    """The five-feature model stays as it is. The residual copy adds the baseline score."""
    import numpy as np

    from src.trace_eval import TraceSpec, evaluate_traces, synthetic_traces
    from src.trace_graph import NODE_FEATURES, CallGraph, with_baseline_feature

    graph = CallGraph(
        ["api", "db"],
        np.ones((2, 5)),
        np.zeros((2, 0), dtype=np.int64),
        np.zeros((0, 3)),
        {"api": 0.2, "db": 1.5},
        {"api": 0.4, "db": 0.1},
    )
    extra = with_baseline_feature(graph)
    assert len(NODE_FEATURES) == 5
    assert graph.node_x.shape == (2, 5)
    assert extra.node_x.shape == (2, 6)
    assert extra.node_x[0, 5] == pytest.approx(0.4)
    assert extra.node_x[1, 5] == pytest.approx(1.5)
    assert extra.services == ["api", "db"]

    widths: list[int] = []

    def fake_fit(_spans, faults, _train_end, **kwargs):
        graphs = kwargs.get("graphs")
        width = 5 if not graphs else int(next(iter(graphs.values())).node_x.shape[1])
        widths.append(width)

        class _Archive:
            backend = "plain"
            device = "cpu"
            fell_back = False
            gnn_fell_back = False

        archive = _Archive()
        archive.width = width
        return archive, "trained"

    def fake_predict(archive, _spans, faults, **_kwargs):
        if archive.width == 6:
            return {fault.fault_id: fault.component for fault in faults}
        return {fault.fault_id: "other" for fault in faults}

    monkeypatch.setattr("src.trace_eval.fit_trace_model", fake_fit)
    monkeypatch.setattr("src.trace_eval.predict_services", fake_predict)
    spans, metrics, faults = synthetic_traces(seed=0)
    report = evaluate_traces(
        spans,
        metrics,
        faults,
        config=Config(),
        spec=TraceSpec(epochs=1, seed=0, backend="plain", budget_ks=(20,), headline_k=20),
    )
    assert widths == [5, 6]
    assert report["headline"]["trace_model"]["faults_caught"] == 0
    assert report["headline"]["trace_model"]["decision"] == "drop"
    assert report["headline"]["trace_residual"]["faults_caught"] == report["test_cases"]
    assert "against_baseline" in report["headline"]["trace_residual"]
    assert report["pages"] is False


def test_rcaeval_report_has_suite_system_and_pooled_counts(monkeypatch):
    import numpy as np

    from run_trace_eval import Recording, score_recordings
    from src.trace_eval import TraceSpec, suite_day
    from src.trace_graph import CallGraph

    monkeypatch.setattr("src.trace_model.torch_stack_available", lambda: False)

    def recording(suite: str, system: str, local_day: int) -> Recording:
        start = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=suite_day(local_day, suite), hours=1)
        fault = Fault(f"{suite}-{system}-{local_day}", start, start + pd.Timedelta(minutes=20), "db", "cpu")
        graph = CallGraph(
            ["db"],
            np.zeros((1, 5)),
            np.zeros((2, 0), dtype=np.int64),
            np.zeros((0, 3)),
            {"db": 1.0},
            {"db": 0.0},
        )
        return Recording(suite, system, local_day, fault, pd.DataFrame(), [], graph)

    rows = [
        recording(suite, system, day)
        for suite in ("RE2", "RE3")
        for system in ("ob", "tt")
        for day in (0, 8)
    ]
    report = score_recordings(
        rows,
        spec=TraceSpec(epochs=1, seed=1, backend="plain", budget_ks=(20,), headline_k=20),
        config=Config(),
    )
    assert set(report["by_suite"]) == {"RE2", "RE3"}
    assert set(report["by_system"]) == {
        "RE2/Online Boutique",
        "RE2/Train Ticket",
        "RE3/Online Boutique",
        "RE3/Train Ticket",
    }
    assert report["by_suite"]["RE2"]["train_cases"] == 2
    assert report["by_suite"]["RE2"]["test_cases"] == 2
    assert report["by_suite"]["RE2"]["tiny_train"] is True
    assert report["by_system"]["RE3/Train Ticket"]["train_cases"] == 1
    assert report["by_system"]["RE3/Train Ticket"]["tiny_train"] is True
    assert report["pooled"]["train_cases"] == 4
    assert report["pooled"]["test_cases"] == 4
    assert report["train_cases"] == 4
    assert report["headline"]["trace_baseline"]["faults_caught"] == 4
    assert report["headline"]["queue"]["faults_caught"] == 0
    assert report["headline"]["trace_model"]["decision"] == "skipped"
    assert report["headline"]["trace_residual"]["decision"] == "skipped"
    assert "catch_ids" not in report["by_suite"]["RE2"]
    assert report["pages"] is False
    assert report["flags_changed"] is False
