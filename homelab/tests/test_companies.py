from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homelab.companies import load_companies, select_companies
from homelab.loadprof import replicas_for
from homelab.names import service_from_pod, tenant_source
from homelab.versions import REQUIRED, load_versions


def test_default_companies_and_disabled_manifest():
    companies = load_companies()
    enabled = [company.id for company in companies if company.enabled]
    assert enabled == ["acme", "borealis", "cassini"]
    draco = next(company for company in companies if company.id == "draco")
    assert draco.enabled is False
    assert draco.app == "manifest"
    assert draco.manifest_url == ""
    with pytest.raises(ValueError, match="manifest_url"):
        select_companies(companies, "draco")
    assert [company.id for company in select_companies(companies, "acme")] == ["acme"]


def test_versions_file_has_the_pins():
    found = load_versions()
    for key in REQUIRED:
        assert found[key]


def test_service_names_drop_replica_and_helm_prefix():
    boutique = ["frontend", "checkoutservice", "redis-cart", "emailservice"]
    assert service_from_pod("frontend-7k2ab-pq9xz", boutique) == "frontend"
    assert service_from_pod("redis-cart-0", boutique) == "redis-cart"
    otel = ["frontend", "product-catalog", "checkout"]
    assert service_from_pod("borealis-frontend-7k2ab-pq9xz", otel) == "frontend"
    assert service_from_pod("borealis-product-catalog-aaaaa-bbbbb", otel) == "product-catalog"
    assert tenant_source("acme", "frontend") == "acme/frontend"
    assert tenant_source("acme", "acme/frontend") == "acme/frontend"


def test_load_profiles_follow_the_clock():
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    morning = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    night = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert replicas_for("steady", noon, None) == (1, None)
    assert replicas_for("daily", noon, None)[0] == 3
    assert replicas_for("daily", morning, None)[0] == 2
    assert replicas_for("daily", night, None)[0] == 1
    burst_at = datetime(2026, 1, 1, 6, 2, tzinfo=timezone.utc)
    replicas, until = replicas_for("bursty", burst_at, None)
    assert replicas == 4
    assert until == burst_at + timedelta(minutes=10)
    assert replicas_for("bursty", burst_at + timedelta(minutes=3), until)[0] == 4
    quiet = datetime(2026, 1, 1, 7, 2, tzinfo=timezone.utc)
    assert replicas_for("bursty", quiet, None)[0] == 1
    with pytest.raises(ValueError):
        replicas_for("weekend", noon, None)
