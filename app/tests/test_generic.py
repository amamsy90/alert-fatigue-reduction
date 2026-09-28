"""Any data: column detection, tuning on the early part, the report, the app flow."""
import json

import numpy as np
import pandas as pd

from src.generic import detect, evaluate, live_setting, load_events, load_incidents, load_metrics, setting_from_dict

T0 = pd.Timestamp("2026-01-01", tz="UTC")


def _shop(tmp_path, days=14, seed=3):
    rng = np.random.default_rng(seed)
    stamps = pd.date_range(T0, T0 + pd.Timedelta(days=days), freq="5min", inclusive="left")
    rows, incidents = [], []
    for region in ("eu", "us"):
        values = rng.poisson(100, len(stamps)).astype(float)
        for d in range(1, days, 2):
            start = T0 + pd.Timedelta(days=d, hours=12)
            mask = (stamps >= start) & (stamps < start + pd.Timedelta(minutes=30))
            values[mask] = rng.poisson(10, mask.sum())
            incidents.append((start, start + pd.Timedelta(minutes=30), region))
        rows.append(pd.DataFrame({"time": stamps, "orders": values, "region": region}))
    pd.concat(rows).to_csv(tmp_path / "m.csv", index=False)
    pd.DataFrame(incidents, columns=["start", "end", "region"]).to_csv(tmp_path / "i.csv", index=False)
    return tmp_path / "m.csv", tmp_path / "i.csv"


def test_columns_are_detected_in_long_and_wide_tables(tmp_path):
    long = pd.DataFrame({"time": ["2026-01-01 00:00", "2026-01-01 00:05"], "orders": [1, 2], "region": ["eu", "eu"]})
    frame, mapping = load_metrics(long)
    assert mapping == {"layout": "long", "timestamp": "time", "value": "orders", "series": "region"}
    assert set(frame["series_id"]) == {"eu"}
    wide = pd.DataFrame({"ts": [1700000000, 1700000060], "cpu_a": [1.0, 2.0], "cpu_b": [3.0, 4.0]})
    frame, mapping = load_metrics(wide)
    assert mapping["layout"] == "wide" and set(frame["series_id"]) == {"cpu_a", "cpu_b"}
    assert str(frame["timestamp"].dt.tz) == "UTC"


def test_events_and_incidents_find_the_series_column_by_its_values():
    events = pd.DataFrame({"when": ["2026-01-01 10:00"], "region": ["eu"], "level": ["ERROR"], "text": ["x"]})
    alerts, mapping = load_events(events, {"eu", "us"})
    assert alerts[0].source == "eu" and alerts[0].severity == "error" and mapping["source"] == "region"
    inc = pd.DataFrame({"start": ["2026-01-01 10:00"], "where": ["us"]})
    faults, mapping = load_incidents(inc, 15, {"eu", "us"})
    assert faults[0].component == "us" and faults[0].end - faults[0].start == pd.Timedelta(minutes=15)
    everywhere, _ = load_incidents(pd.DataFrame({"start": ["2026-01-01 10:00"]}), 10, {"eu", "us"})
    assert {f.component for f in everywhere} == {"eu", "us"}


def test_evaluate_tunes_on_the_early_part_and_beats_chance(tmp_path):
    metrics, incidents = _shop(tmp_path)
    report = evaluate(metrics, incidents, budgets=(3,), train_fraction=0.4).to_dict()
    assert report["summary"]["train_incidents"] > 0 and report["summary"]["test_incidents"] > 0
    chosen = report["settings"]["chosen"]
    assert report["settings"][chosen]["tuned"]
    mine = next(r for r in report["table"] if r["queue"] == "your_queue")
    assert mine["detection_rate"] >= 0.8
    assert mine["detection_rate"] > 5 * report["checks"]["chance_detection"]


def test_miss_causes_match_your_queue_at_the_largest_budget(tmp_path):
    """The miss paragraph must add up to the largest-K row, not the last budget typed."""
    metrics, incidents = _shop(tmp_path)
    report = evaluate(metrics, incidents, budgets=(50, 5), train_fraction=0.4).to_dict()
    assert report["misses"]["budget_k"] == 50
    mine = next(r for r in report["table"] if r["queue"] == "your_queue" and r["budget_k"] == 50)
    missed = mine["faults"] - mine["faults_caught"]
    causes = report["misses"]
    assert causes["no_data"] + causes["no_detection"] + causes["budget_full"] == missed


def test_an_alert_catch_is_not_called_a_miss(tmp_path):
    from src.entities import Alert, Fault
    from src.generic import _miss_causes
    from src.queue import QueueItem

    t0 = pd.Timestamp("2026-01-02 12:00", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": [t0, t0 + pd.Timedelta(minutes=5)],
        "value": [1.0, 1.0],
        "series_id": ["eu", "eu"],
    })
    fault = Fault("f1", t0, t0 + pd.Timedelta(minutes=10), "eu")
    alert = Alert("a1", t0 + pd.Timedelta(minutes=1), "eu", "log", "error", "down", "f1")
    shown = [QueueItem(alert.timestamp, "alert", "eu", alert)]
    causes = _miss_causes(frame, [fault], [], shown)
    assert causes == {"no_data": 0, "no_detection": 0, "budget_full": 0}


def test_without_incidents_the_report_says_so(tmp_path):
    metrics, _ = _shop(tmp_path)
    report = evaluate(metrics, None, budgets=(3,)).to_dict()
    assert report["summary"]["incidents"] == 0
    assert any("No incidents" in n for n in report["notes"])


def test_saved_setting_scores_new_data_the_same_way(tmp_path):
    metrics, incidents = _shop(tmp_path)
    report = evaluate(metrics, incidents, budgets=(3,), train_fraction=0.4)
    saved = json.loads(json.dumps(live_setting(metrics, report), default=str))
    frame, _ = load_metrics(metrics)
    found = detect(frame, setting_from_dict(saved), {})
    assert len(found) >= 10


def test_robust_mode_catches_drops_in_any_unit():
    from src.config import Config
    from src.memory_spikes import detect_jumps

    stamps = pd.date_range(T0, periods=200, freq="1min")
    values = np.r_[np.full(150, 50.0) + np.sin(np.arange(150)), np.full(10, 5.0), np.full(40, 50.0)]
    frame = pd.DataFrame({"timestamp": stamps, "value": values, "service": "s", "series_id": "s"})
    cfg = Config(memory_jump_mode="robust", robust_z=5, jump_direction="both", memory_gap_minutes=0)
    (jump,) = [j for j in detect_jumps(frame, cfg) if j.start >= T0 + pd.Timedelta(minutes=150)][:1]
    assert jump.start == T0 + pd.Timedelta(minutes=150)
    assert detect_jumps(frame, Config(memory_jump_mode="robust", jump_direction="up", memory_gap_minutes=0)) == [] or all(
        j.start < T0 + pd.Timedelta(minutes=150) or j.start >= T0 + pd.Timedelta(minutes=160)
        for j in detect_jumps(frame, Config(memory_jump_mode="robust", jump_direction="up", memory_gap_minutes=0)))


def test_cli_evaluate_and_tune(tmp_path, monkeypatch, capsys):
    from product.cli import main

    metrics, incidents = _shop(tmp_path)
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert main(["evaluate", "--metrics", str(metrics), "--incidents", str(incidents), "--budgets", "3",
                 "--train-fraction", "0.4", "--out", str(tmp_path / "r.json")]) == 0
    assert "your_queue" in capsys.readouterr().out
    assert json.loads((tmp_path / "r.json").read_text())["summary"]["series"] == 2
    assert main(["tune", "--metrics", str(metrics), "--incidents", str(incidents), "--budgets", "3",
                 "--train-fraction", "0.4"]) == 0
    assert json.loads((tmp_path / "settings.json").read_text())["detector"]["kind"] in ("jump", "delta", "static")


def test_app_evaluate_apply_and_score(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import product.app as app_mod
    from product.store import QueueStore

    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setattr("product.web.context.STORE", QueueStore(tmp_path / "q.db"))
    monkeypatch.setattr("product.web.evaluate_routes.EVAL_DIR", tmp_path / "evals")
    client = TestClient(app_mod.app)
    metrics, incidents = _shop(tmp_path)
    with open(metrics, "rb") as m, open(incidents, "rb") as i:
        res = client.post("/evaluate", files={"metrics_file": ("m.csv", m), "incidents_file": ("i.csv", i)},
                          data={"budgets": "3", "train_fraction": "0.4"})
    assert res.status_code == 200, res.text
    eval_id = res.json()["eval_id"]
    assert client.get("/settings").json()["saved"] is False
    applied = client.post("/settings", json={"eval_id": eval_id}).json()
    assert applied["saved"] and applied["detector"]["kind"]
    job = client.post(f"/evaluate/{eval_id}/score", json={"budget_k": 3})
    assert job.status_code == 200, job.text
    meta = job.json()["meta"]
    assert meta["jumps_shown"] >= 10 and meta["detector"] == applied["label"]
    assert client.get("/queue", params={"job_id": job.json()["job_id"]}).json()["jumps"]
    assert client.delete("/settings").json()["saved"] is False
    assert client.post("/evaluate", data={"dataset": "nope"}).status_code == 400
    assert client.post("/evaluate", data={}).status_code == 400
    assert client.get("/evaluate").status_code == 200
