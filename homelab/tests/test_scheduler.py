from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homelab.chaos import Choice
from homelab.companies import load_companies
from homelab.scheduler import _rng, _store_rng, apply_load, close_open, due, load_state, open_fault, save_state


def test_rng_survives_a_restart(tmp_path):
    acme = next(company for company in load_companies() if company.id == "acme")
    path = tmp_path / "state.json"
    state = load_state(path, [acme])
    rng = _rng(state, "acme")
    rng.random()
    _store_rng(state, "acme", rng)
    expected = rng.random()
    save_state(path, state)
    restored = _rng(load_state(path, [acme]), "acme")
    assert restored.random() == expected


def test_open_fault_records_the_pod_kill_window():
    acme = next(company for company in load_companies() if company.id == "acme")
    state = load_state_only(acme)
    start = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
    choice = Choice("pod-kill", "frontend", timedelta(minutes=5), timedelta(minutes=2))
    row = open_fault(state, acme, choice, start)
    assert row["company"] == "acme"
    assert row["component"] == "acme/frontend"
    assert row["status"] == "running"
    end = datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
    assert end - start == timedelta(minutes=2)
    assert due(state, acme, start + timedelta(minutes=1)) == "wait"
    assert due(state, acme, start + timedelta(minutes=2)) == "close"
    close_open(state, acme, start + timedelta(minutes=2), "closed")
    assert state["faults"][0]["status"] == "closed"
    assert state["companies"]["acme"]["open"] is None


def test_load_scale_is_printed_and_not_executed(capsys):
    acme = next(company for company in load_companies() if company.id == "acme")
    borealis = next(company for company in load_companies() if company.id == "borealis")
    state = {"companies": {"acme": {"burst_until": None}, "borealis": {"burst_until": None}}}
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert apply_load(state, acme, noon, True) == 1
    assert apply_load(state, borealis, noon, True) == 3
    printed = capsys.readouterr().out
    assert "--replicas=1" in printed
    assert "--replicas=3" in printed
    assert "kubectl " in printed


def load_state_only(company):
    return {
        "faults": [],
        "companies": {company.id: {"seed": 1, "next_start": "2026-01-01T00:00:00Z", "open": None, "burst_until": None}},
    }


def test_not_injected_fault_is_not_closed():
    from datetime import datetime, timedelta, timezone

    from homelab.scheduler import close_open

    class C:
        id = "acme"

    start = datetime(2026, 10, 21, 19, 0, tzinfo=timezone.utc)
    row = {"fault_type": "network-delay", "service": "frontend",
           "end": (start + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")}
    state = {"companies": {"acme": {"open": row}}}
    closed = close_open(state, C(), start + timedelta(minutes=5), "not-injected")
    assert closed["status"] == "not-injected"
    assert "not injected" in closed["description"]
    assert state["companies"]["acme"]["open"] is None


def test_close_after_restart_updates_the_fault_list(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    from homelab.scheduler import close_open

    class C:
        id = "acme"

    start = datetime(2026, 10, 21, 19, 0, tzinfo=timezone.utc)
    row = {"fault_id": "f1", "fault_type": "pod-kill", "service": "frontend", "status": "running",
           "end": (start + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")}
    state = json.loads(json.dumps({"faults": [row], "companies": {"acme": {"open": row}}}))
    close_open(state, C(), start + timedelta(minutes=3), "closed")
    assert state["faults"][0]["status"] == "closed"
