"""Shared test setup.

The app reads login, proxy, ingest-limit and connector settings from the
environment on every request. A developer who has, say, APP_PASSWORD exported
in their shell would otherwise see dozens of 401s. Every test starts with
these unset; a test that needs one sets it with monkeypatch.setenv.
"""
from __future__ import annotations

import pytest

CLEARED_ENV = (
    # Sign-in and roles
    "APP_PASSWORD",
    "APP_USER",
    "APP_VIEWER_PASSWORD",
    "APP_VIEWER_USER",
    "ALLOW_OPEN_ACCESS",
    "INGEST_TOKEN",
    "COOKIE_SECURE",
    "LOGIN_MAX_FAILS",
    "LOGIN_LOCK_SECONDS",
    # Reverse proxy and Host checks
    "TRUSTED_PROXIES",
    "ALLOWED_HOSTS",
    # Ingest and upload limits
    "INGEST_MAX_BATCHES",
    "INGEST_MAX_ALERTS",
    "INGEST_WINDOW_SECONDS",
    "MAX_UPLOAD_MB",
    "MAX_INGEST_MB",
    # Paging and connectors
    "SHADOW_MODE",
    "PAGERDUTY_ROUTING_KEY",
    "SLACK_WEBHOOK_URL",
    "OPSGENIE_API_KEY",
    "OPSGENIE_API_URL",
    "PROM_URL",
    "PROM_QUERY",
    "PROM_SERVICE_LABEL",
    "PROM_ALLOW_PRIVATE",
    # Optional models. Unset means the default queue and the base note path.
    "TRACE_MODEL",
    "TRACE_MODEL_DIR",
    "TRACE_MODEL_SHA256",
    "LLM_LORA",
    "LLM_LORA_DIR",
    "LLM_LORA_SHA256",
    "LLM_LORA_BASE",
    "LOG_ENCODER",
    "LOG_ENCODER_PATH",
    "LOG_ENCODER_SHA256",
    "LOG_ENCODER_DIM",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name in CLEARED_ENV:
        monkeypatch.delenv(name, raising=False)
    # Sessions and login locks are kept on disk; each test gets its own file.
    monkeypatch.setenv("AUTH_DB", str(tmp_path / "auth.db"))
