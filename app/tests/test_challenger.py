"""Champion and challenger tallies: expected counts are worked out by hand from the cost definition."""
from __future__ import annotations

import pandas as pd
import pytest

from src.challenger import MIN_JOBS, MIN_LABELLED, accumulate, compare, recommend
from src.entities import Action, Alert, Decision

T0 = pd.Timestamp("2026-03-02", tz="UTC")


def _alert(i, fault=None):
    return Alert(f"a{i}", T0 + pd.Timedelta(minutes=i), "web", "log", "error", "m", fault)


def _dec(i, action):
    return Decision(f"a{i}", action, 0.5, "")


ESC, REV, SUP = Action.ESCALATE, Action.REVIEW, Action.SUPPRESS
COSTS = {"miss_cost": 10.0, "false_alarm_cost": 1.0}


def test_counts_and_cost_by_hand():
    # alert:        0 fault  1 fault  2 noise  3 noise  4 fault(unlabelled)
    alerts = [_alert(0, "f"), _alert(1, "f"), _alert(2), _alert(3), _alert(4, "f")]
    labelled = [True, True, True, True, False]
    champion = [_dec(0, ESC), _dec(1, SUP), _dec(2, ESC), _dec(3, SUP), _dec(4, SUP)]
    challenger = [_dec(0, ESC), _dec(1, ESC), _dec(2, SUP), _dec(3, SUP), _dec(4, ESC)]
    out = compare(champion, challenger, alerts, labelled, **COSTS)
    # Champion: misses alert 1 (a fault it did not escalate) and escalates noise alert 2 -> 10 + 1.
    assert (out["champion_fn"], out["champion_fp"], out["champion_cost"]) == (1, 1, 11.0)
    # Challenger catches both faults and escalates no noise -> 0. Alert 4 is unlabelled and never counted.
    assert (out["challenger_fn"], out["challenger_fp"], out["challenger_cost"]) == (0, 0, 0.0)
    assert out["labelled"] == 4 and out["alerts"] == 5
    # Surfaced (escalate or review) disagreements: alert 1 and alert 4 challenger-only, alert 2 champion-only.
    assert (out["agree"], out["champion_only"], out["challenger_only"]) == (2, 1, 2)


def test_review_is_shown_but_is_not_an_escalation_for_cost():
    alerts = [_alert(0, "f")]
    out = compare([_dec(0, REV)], [_dec(0, SUP)], alerts, [True], **COSTS)
    assert out["champion_only"] == 1          # shown by the champion only
    assert out["champion_fn"] == 1 and out["challenger_fn"] == 1  # neither escalated the fault


def test_length_mismatch_is_refused():
    with pytest.raises(ValueError):
        compare([_dec(0, ESC)], [], [_alert(0)], [True], **COSTS)


def _job(champ, chall, labelled=10):
    return {"alerts": 20, "labelled": labelled, "champion_cost": champ, "challenger_cost": chall,
            "champion_fn": 0, "champion_fp": 0, "challenger_fn": 0, "challenger_fp": 0}


def test_accumulate_sums_counts_and_tracks_wins():
    tally = accumulate(None, _job(10, 0))
    tally = accumulate(tally, _job(0, 5))
    tally = accumulate(tally, _job(4, 4))
    tally = accumulate(tally, _job(7, 7, labelled=0))  # nothing labelled: counted as a job, never judged
    assert tally["jobs"] == 4 and tally["judged_jobs"] == 3
    assert (tally["challenger_wins"], tally["champion_wins"]) == (1, 1)
    assert tally["champion_cost"] == 21 and tally["challenger_cost"] == 16 and tally["labelled"] == 30


def test_too_little_evidence_keeps_collecting():
    tally = accumulate(accumulate(None, _job(100, 0)), _job(100, 0))
    assert recommend(tally, margin=10)[0] == "collecting"
    few_labels = None
    for _ in range(MIN_JOBS):
        few_labels = accumulate(few_labels, _job(100, 0, labelled=1))
    assert few_labels["labelled"] < MIN_LABELLED
    assert recommend(few_labels, margin=10)[0] == "collecting"


def test_promote_needs_the_margin_and_more_wins_than_losses():
    tally = None
    for champ, chall in ((20, 5), (20, 5), (20, 5)):
        tally = accumulate(tally, _job(champ, chall, labelled=MIN_LABELLED))
    assert recommend(tally, margin=10)[0] == "promote"
    assert recommend(tally, margin=60)[0] == "keep"  # saves 45, below a margin of 60


def test_one_big_win_cannot_outvote_the_job_record():
    """Saves 40 in total but loses two of three jobs: keep."""
    tally = None
    for champ, chall in ((100, 0), (1, 2), (1, 2)):
        tally = accumulate(tally, _job(champ, chall, labelled=MIN_LABELLED))
    status, reason = recommend(tally, margin=10)
    assert status == "keep", reason


# --- through the app -------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    for name in ("QUEUE_DB", "MODEL_PATH", "SETTINGS_PATH", "OPS_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    for name in ("APP_PASSWORD", "ALLOW_OPEN_ACCESS"):
        monkeypatch.delenv(name, raising=False)
    import product.app as app_mod
    from product.store import QueueStore

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    return TestClient(app_mod.app)


def _upload():
    from tests.test_model_registry import _labelled_upload

    return _labelled_upload()


def test_a_refused_model_is_tallied_against_the_champion_without_changing_it(client, monkeypatch):
    import product.pipeline as pipeline
    from product.model_store import load_registry

    first = client.post("/jobs/upload", files=_upload(), data={"budget_k": "20"}).json()["meta"]
    assert first["training"]["promoted"] and "challenger" not in first  # nothing to compare yet
    monkeypatch.setattr(pipeline, "promotion_decision", lambda *a, **k: (False, "forced refusal"))
    second = client.post("/jobs/upload", files=_upload(), data={"budget_k": "20"}).json()["meta"]
    assert second["training"]["model_version"] == "v0002" and "challenger" not in second  # v0002 only now exists
    third = client.post("/jobs/upload", files=_upload(), data={"budget_k": "20"}).json()["meta"]
    info = third["challenger"]
    assert info["version"] == "v0002" and info["jobs"] == 1
    assert info["this_job"]["alerts"] > 0 and info["this_job"]["labelled"] == info["this_job"]["alerts"]
    assert info["recommendation"] == "collecting"
    assert client.get("/models").json()["active"] == "v0001"  # the tally never promotes
    assert load_registry()["versions"][1]["shadow"]["jobs"] == 1
    fourth = client.post("/jobs/upload", files=_upload(), data={"budget_k": "20"}).json()["meta"]
    assert fourth["challenger"]["version"] == "v0003"  # the newest refused model takes over
