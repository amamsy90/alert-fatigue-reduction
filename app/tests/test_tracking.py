"""The experiment log records code, config, data, outputs, metrics and the reason."""
from __future__ import annotations

from src.config import Config
from src.tracking import fingerprint, read_log, record_run


def test_a_run_is_recorded_with_everything_needed_to_trace_it(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPERIMENT_NOTE", "after the look-ahead fix")
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.csv").write_text("x\n1\n")
    out = tmp_path / "result.csv"
    out.write_text("queue,rate\np,0.7\n")
    log = tmp_path / "experiments.jsonl"
    entry = record_run("demo", config=Config(budget_k=7), data=[data], outputs=[out],
                       metrics={"K20.proposed": 0.69583}, log=log)
    assert read_log(log) == [entry]
    assert entry["note"] == "after the look-ahead fix"
    assert entry["config"]["budget_k"] == 7
    assert entry["metrics"]["K20.proposed"] == 0.69583
    assert set(entry["outputs"]) == {"result.csv"} and entry["data"]
    assert "commit" in entry["code"] and entry["libraries"]["pandas"]
    record_run("demo", log=log)
    assert len(read_log(log)) == 2  # appended, never rewritten


def test_the_data_fingerprint_changes_when_the_inputs_change(tmp_path):
    folder = tmp_path / "d"
    folder.mkdir()
    (folder / "a.csv").write_text("1\n")
    before = fingerprint([folder])
    assert fingerprint([folder]) == before
    (folder / "b.csv").write_text("2\n")
    assert fingerprint([folder]) != before
