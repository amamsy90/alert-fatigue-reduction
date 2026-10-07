"""Request bodies accepted by the API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    mode: str = Field("sample", description="sample | gaia | ingest")
    budget_k: int = 20
    shadow_mode: bool | None = None


class IngestAlert(BaseModel):
    alert_id: str | None = None
    timestamp: str
    source: str
    alert_type: str | None = "info"
    severity: str = "info"
    message: str = ""
    fault_id: str | None = None


class FeedbackRequest(BaseModel):
    kind: str = Field(..., description="alert | jump")
    id: int
    verdict: str | None = Field(None, description="real | noise | null to clear")


class ShadowModeRequest(BaseModel):
    shadow_mode: bool = Field(..., description="true = Shadow (safe); false = Live")
    confirm: str | None = Field(
        None, description="Must be exactly LIVE when turning Live on (shadow_mode=false)"
    )


class RoutingKeyRequest(BaseModel):
    routing_key: str = Field(..., description="PagerDuty Events API v2 routing key")


class SlackWebhookRequest(BaseModel):
    webhook_url: str = Field(..., description="Slack incoming-webhook URL (https://hooks.slack.com/...)")


class OpsgenieKeyRequest(BaseModel):
    api_key: str = Field(..., description="Opsgenie API-integration key")


class PromUrlRequest(BaseModel):
    url: str = Field(..., description="Prometheus base URL, e.g. http://localhost:9090")


class IngestRequest(BaseModel):
    alerts: list[IngestAlert]
    score_now: bool = False
    budget_k: int = 20


class ResetRequest(BaseModel):
    confirm: str = Field(..., description="Must be exactly RESET")
