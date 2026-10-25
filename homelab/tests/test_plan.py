from __future__ import annotations

import json

import yaml
from homelab.companies import load_companies, select_companies
from homelab.obs import collector_config, render
from homelab.plan import loadgenerator_manifest, otel_demo_values, rewrite_namespace
from homelab.plan import render as render_plan
from homelab.safety import assert_shadow_manifest
from homelab.versions import load_versions
from homelab.workloads import actions_for


def test_namespace_rewrite_and_render(tmp_path):
    text = "namespace: sock-shop\nmetadata:\n  name: sock-shop\n"
    rewritten = rewrite_namespace(text, "sock-shop", "company-cassini")
    assert "sock-shop" not in rewritten
    assert "company-cassini" in rewritten
    companies = select_companies(load_companies(), "acme,borealis,cassini")
    plan = render_plan(companies, tmp_path)
    assert_shadow_manifest((tmp_path / "oncall-queue.yaml").read_text(encoding="utf-8"))
    assert plan["webhook"].endswith("/ingest/alertmanager")
    assert "score_now" not in (tmp_path / "oncall-queue.yaml").read_text(encoding="utf-8")
    rules = list(yaml.safe_load_all((tmp_path / "rules.yaml").read_text(encoding="utf-8")))
    assert len([doc for doc in rules if doc]) == 3
    saved = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    versions = load_versions()
    otel = next(step for step in saved["steps"] if step["company"] == "borealis")
    assert otel["version"] == versions["OTEL_DEMO_CHART_VERSION"]
    load = loadgenerator_manifest(next(company for company in companies if company.id == "cassini"))
    assert versions["CURL_IMAGE"] in load
    assert "company-cassini" in load
    borealis = next(company for company in companies if company.id == "borealis")
    otel_values = yaml.safe_load((tmp_path / f"otel-demo-{borealis.id}.yaml").read_text(encoding="utf-8"))
    checkout = otel_values["components"]["checkout"]
    assert checkout["initContainers"] == []
    assert checkout["envOverrides"] == [{"name": "KAFKA_ADDR", "value": ""}]
    assert otel_values["components"]["kafka"]["enabled"] is False
    assert "company=borealis" in otel_demo_values(borealis)


def test_collector_binds_otlp_and_tags_each_company():
    companies = select_companies(load_companies(), "acme,cassini")
    config = collector_config(companies)
    assert "0.0.0.0:4317" in config
    assert "0.0.0.0:4318" in config
    assert "0.0.0.0:8889" in config
    assert 'company-acme' in config
    assert 'company-cassini' in config
    assert "traces.spanmetrics" in config
    assert "sampling_percentage: 10" in config


def test_obs_render_uses_pinned_images(tmp_path):
    companies = select_companies(load_companies(), "acme")
    render(companies, tmp_path)
    versions = load_versions()
    loki = (tmp_path / "loki.yaml").read_text(encoding="utf-8")
    tempo = (tmp_path / "tempo.yaml").read_text(encoding="utf-8")
    collector = (tmp_path / "otel-collector.yaml").read_text(encoding="utf-8")
    assert versions["LOKI_IMAGE"] in loki
    assert versions["TEMPO_IMAGE"] in tempo
    assert "0.0.0.0:4317" in tempo
    tempo_deploy = next(
        doc for doc in yaml.safe_load_all(tempo) if doc and doc.get("kind") == "Deployment"
    )
    tempo_resources = tempo_deploy["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert tempo_resources["requests"]["memory"] == "512Mi"
    assert tempo_resources["limits"]["memory"] == "3Gi"
    assert versions["COLLECTOR_IMAGE"] in collector
    assert "company-acme" in collector


def test_workload_labels_and_boutique_tracing():
    companies = {company.id: company for company in load_companies()}
    acme = actions_for(companies["acme"], ["frontend", "loadgenerator"])
    front = next(action for action in acme if action["deployment"] == "frontend")
    labels = front["patch"]["spec"]["template"]["metadata"]["labels"]
    assert labels["service"] == "frontend"
    assert labels["company"] == "acme"
    assert front["env"][0] == ("ENABLE_TRACING", "1")
    load = next(action for action in acme if action["deployment"] == "loadgenerator")
    assert load["load"] is True
    assert "env" not in load
    assert "service" not in load["patch"]["spec"]["template"]["metadata"]["labels"]
    borealis = actions_for(companies["borealis"], ["borealis-frontend", "borealis-load-generator"])
    named = next(action for action in borealis if action["service"] == "frontend")
    assert named["deployment"] == "borealis-frontend"
    assert "env" not in named
    assert any(action["load"] for action in borealis)
