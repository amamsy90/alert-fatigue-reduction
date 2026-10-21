from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from homelab.chaos import FaultRow, choose, gap_minutes, manifest
from homelab.companies import load_companies, select_companies
from homelab.rules import profile, render_all, render_company
from homelab.safety import WEBHOOK_URL, app_manifest, assert_shadow_manifest, assert_shadow_status


def test_chaos_gap_and_pod_kill_record():
    acme = next(company for company in load_companies() if company.id == "acme")
    app = acme.application()
    assert app is not None
    rng = random.Random(1)
    saw_kill = False
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(40):
        choice = choose(acme, app, rng)
        assert choice is not None
        minutes = choice.duration.total_seconds() / 60
        assert acme.chaos.duration_min_minutes <= minutes <= acme.chaos.duration_max_minutes
        gap = gap_minutes(acme, rng)
        assert acme.chaos.interval_min_minutes <= gap <= acme.chaos.interval_max_minutes
        if choice.fault_type == "pod-kill":
            saw_kill = True
            assert choice.recorded == timedelta(minutes=2)
        else:
            assert choice.recorded == choice.duration
        text = manifest(acme, choice, app)
        assert f"namespace: {acme.namespace}" in text
        assert "company: acme" in text
        row = FaultRow(
            "id",
            acme.id,
            choice.service,
            choice.fault_type,
            start,
            start + choice.recorded,
            "running",
        ).as_dict()
        assert row["company"] == "acme"
        assert row["component"] == f"acme/{choice.service}"
        assert "company" in row
    assert saw_kill


def test_noisy_rules_include_cpu_and_strict_rules_do_not():
    companies = {company.id: company for company in load_companies()}
    assert profile("noisy")["include_resources"] is True
    assert profile("strict")["include_resources"] is False
    noisy = render_company(companies["acme"])
    strict = render_company(companies["borealis"])
    assert "HomelabCpuHigh" in noisy
    assert "HomelabMemoryHigh" in noisy
    assert "HomelabCpuHigh" not in strict
    assert "HomelabMemoryHigh" not in strict
    text = render_all([companies["acme"], companies["cassini"], companies["borealis"]])
    docs = [doc for doc in yaml.safe_load_all(text) if doc]
    assert len(docs) == 3
    assert "acme/{{ $labels.service }}" in text
    assert "borealis/{{ $labels.service }}" in text
    assert all(doc["metadata"]["labels"]["company"] for doc in docs)


def test_app_manifest_cannot_page_and_webhook_has_no_score_now():
    text = app_manifest()
    assert_shadow_manifest(text)
    assert "0.0.0.0" not in text
    kps = Path(__file__).resolve().parents[1] / "config" / "kps-values.yaml"
    body = kps.read_text(encoding="utf-8")
    parsed = yaml.safe_load(body)
    assert WEBHOOK_URL == parsed["alertmanager"]["config"]["receivers"][1]["webhook_configs"][0]["url"]
    assert "score_now" not in body
    assert "APP_PASSWORD" not in body
    assert parsed["alertmanager"]["tplConfig"] is False
    assert_shadow_status(
        {
            "shadow_mode": True,
            "live": False,
            "login_required": False,
            "pagerduty": {"configured": False, "will_page": False},
            "slack": {"configured": False, "will_page": False},
            "opsgenie": {"configured": False, "will_page": False},
        }
    )


def test_shadow_status_refuses_a_live_or_paged_app():
    with pytest.raises(ValueError):
        assert_shadow_status({"shadow_mode": False, "live": True})
    with pytest.raises(ValueError):
        assert_shadow_status(
            {
                "shadow_mode": True,
                "live": False,
                "pagerduty": {"configured": True, "will_page": False},
            }
        )


def test_select_unknown_company():
    with pytest.raises(ValueError, match="unknown"):
        select_companies(load_companies(), "not-a-company")
