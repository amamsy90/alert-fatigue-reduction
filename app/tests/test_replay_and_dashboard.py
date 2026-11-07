"""The replay and the Grafana dashboard: each claim is checked against how the product behaves."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

APP = Path(__file__).parents[1]


def test_dashboard_only_uses_metrics_the_app_exports():
    """A panel on a metric nobody exports would sit empty and look like a healthy system."""
    dashboard = json.loads((APP / "deploy" / "grafana" / "oncall-queue.json").read_text(encoding="utf-8"))
    exprs = [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    used = {re.sub(r"_(bucket|sum|count)$", "", m) for e in exprs for m in re.findall(r"oncall_[a-z0-9_]+", e)}
    source = "\n".join(p.read_text(encoding="utf-8") for p in (APP / "product").rglob("*.py"))
    missing = sorted(name for name in used if f'"{name}"' not in source)
    assert not missing, f"not exported by the app: {missing}"
    assert len(used) >= 12


def test_dashboard_panels_fit_the_grid_and_have_titles():
    dashboard = json.loads((APP / "deploy" / "grafana" / "oncall-queue.json").read_text(encoding="utf-8"))
    for panel in dashboard["panels"]:
        assert panel["title"] and panel["targets"]
        pos = panel["gridPos"]
        assert pos["x"] + pos["w"] <= 24  # Grafana's grid is 24 columns wide


@pytest.fixture(scope="module")
def gaia_replay():
    import os

    cwd = os.getcwd()
    os.chdir(APP)  # the replay reads examples/ relative to app/
    try:
        from run_live_replay import replay

        return replay("gaia", accuracy=0.9, marks_per_day=20, seed=0)
    finally:
        os.chdir(cwd)


def test_the_committed_replay_result_is_reproduced(gaia_replay):
    """results/live_replay_gaia.json is what the report cites; a fresh run must give the same file."""
    committed = json.loads((APP / "results" / "live_replay_gaia.json").read_text(encoding="utf-8"))
    assert gaia_replay == committed


def test_the_replay_never_pages_and_never_invents_a_model(gaia_replay):
    first = gaia_replay
    summary = first["summary"]
    # Shadow mode throughout; the log alerts of GAIA carry no learnable signal, so nothing is promoted.
    assert summary["retrains_promoted"] == 0 and summary["active"] is None
    assert summary["days"] == 11 and summary["alerts"] == 4852  # the committed sample, every alert replayed once
    for day in first["days"]:
        assert day["marked"] <= 20  # an engineer reads at most the daily budget


def test_precision_from_marks_is_the_share_of_real_marks(gaia_replay):
    first = gaia_replay
    last = first["days"][-1]
    total = last["marks_real"] + last["marks_noise"]
    assert last["precision_from_marks"] == (round(last["marks_real"] / total, 4) if total else None)
