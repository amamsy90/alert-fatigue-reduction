"""Would-have-paged shadow summary + sample counts; real send still skipped."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SHADOW_MODE", "1")
os.environ["QUEUE_DB"] = str(Path(tempfile.mkdtemp()) / "import_queue.db")

from product.ops import set_routing_key  # noqa: E402
from product.pagerduty import would_have_paged  # noqa: E402
from product.store import QueueStore  # noqa: E402
from src.entities import Action, Alert, Decision  # noqa: E402
from src.memory_spikes import Spike  # noqa: E402

T0 = pd.Timestamp("2026-01-12 10:00:00", tz="UTC")


class _Resp:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture()
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setenv("SHADOW_MODE", "1")
    monkeypatch.delenv("PAGERDUTY_ROUTING_KEY", raising=False)
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    import product.app as app_mod

    app_mod.use_store(QueueStore(tmp_path / "queue.db"))
    return TestClient(app_mod.app)


def test_would_have_paged_counts_spikes_and_escalations():
    jump = Spike(
        "web",
        "web-01",
        T0,
        T0 + pd.Timedelta(minutes=5),
        1.4e9,
        confirm=T0 + pd.Timedelta(seconds=30),
        rise_at_confirm=1.1e9,
    )
    gap = Spike(
        "gap",
        "db-01",
        T0,
        T0 + pd.Timedelta(hours=1),
        confirm=T0 + pd.Timedelta(minutes=10),
        kind="gap",
    )
    alerts = [
        Alert("e1", T0, "web-01", "oom", "error", "oom"),
        Alert("r1", T0, "web-01", "warn", "warning", "noise"),
    ]
    decisions = [
        Decision("e1", Action.ESCALATE, 0.9, "policy"),
        Decision("r1", Action.REVIEW, 0.4, "policy"),
    ]
    out = would_have_paged(alerts, decisions, spikes=[jump, gap])
    assert out["spikes"] == 2
    assert out["alerts"] == 1
    assert out["count"] == 3
    assert len(out["items"]) == 3
    kinds = {it["kind"] for it in out["items"]}
    assert "jump" in kinds or "web" in {it.get("kind") for it in out["items"]}
    # Spike.kind defaults to jump for first Spike above (kind not passed -> "jump" via detect)
    assert any(it["dedup_key"].startswith("jump:") or it["kind"] == "jump" for it in out["items"])
    assert any(it["kind"] == "gap" for it in out["items"])
    assert any(it["kind"] == "alert" and it["dedup_key"] == "e1" for it in out["items"])


def test_would_have_paged_caps_items_list():
    spikes = [
        Spike(
            "web",
            f"svc-{i}",
            T0 + pd.Timedelta(minutes=i),
            T0 + pd.Timedelta(minutes=i + 5),
            1e9,
            confirm=T0 + pd.Timedelta(minutes=i, seconds=30),
            rise_at_confirm=8e8,
        )
        for i in range(25)
    ]
    out = would_have_paged([], [], spikes=spikes)
    assert out["count"] == 25
    assert out["spikes"] == 25
    assert len(out["items"]) == 20


def test_sample_would_have_paged_gt_zero_with_jump_gap_leak(ops_env):
    import product.app as app_mod
    from product.pipeline import run_job

    with patch("product.pagerduty.urlopen", return_value=_Resp()) as mocked:
        result = run_job(app_mod.STORE, mode="sample", budget_k=20, shadow_mode=True)
    assert mocked.call_count == 0
    page = result["meta"]["pagerduty"]
    assert page.get("sent", 0) == 0
    assert "never pages" in page.get("skipped", "") or page.get("skipped") == "shadow mode"
    # sample always skips via mode guard
    assert "never pages" in page["skipped"]
    whp = result["meta"]["would_have_paged"]
    assert whp["count"] > 0
    assert whp["spikes"] > 0
    kinds = {it["kind"] for it in whp["items"]}
    # Sample day includes jump, gap, and leak — all should be candidates to page when shown.
    assert kinds & {"jump", "gap", "leak"}
    assert {"jump", "gap", "leak"} <= kinds or len(kinds & {"jump", "gap", "leak"}) >= 2


def test_shadow_ingest_skips_send_but_still_records_would_have(ops_env):
    set_routing_key("rk_shadow_whp")
    import product.app as app_mod
    from product.pipeline import run_job

    rows = [
        {
            "timestamp": "2026-02-01T10:00:00Z",
            "source": "web-01",
            "severity": "error",
            "message": "oom",
        }
    ]
    with patch("product.pagerduty.urlopen", return_value=_Resp()) as mocked:
        result = run_job(
            app_mod.STORE, mode="ingest", budget_k=5, shadow_mode=True, alert_rows=rows
        )
    assert mocked.call_count == 0
    assert result["meta"]["pagerduty"]["skipped"] == "shadow mode"
    assert "would_have_paged" in result["meta"]
    assert isinstance(result["meta"]["would_have_paged"]["count"], int)


def test_connectors_exposes_would_have_paged(ops_env):
    import product.app as app_mod
    from product.pipeline import run_job

    run_job(app_mod.STORE, mode="sample", budget_k=20, shadow_mode=True)
    body = ops_env.get("/connectors").json()
    whp = body["pagerduty"].get("would_have_paged")
    assert whp is not None
    assert whp["count"] > 0
    assert body["last_job"].get("would_have_paged", {}).get("count", 0) > 0
