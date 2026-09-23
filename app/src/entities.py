"""Core entities for the alert-fatigue system (design spec section 3.2)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Action(str, Enum):
    ESCALATE = "escalate"
    SUPPRESS = "suppress"
    REVIEW = "review"  # routed to a human (low confidence) — counts as surfaced


SURFACED_ACTIONS = {Action.ESCALATE, Action.REVIEW}


@dataclass
class Alert:
    alert_id: str
    timestamp: pd.Timestamp
    source: str
    alert_type: str
    severity: str
    message: str
    fault_id: str | None = None  # ground-truth label, set by alignment


@dataclass
class Fault:
    fault_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    component: str
    description: str = ""


@dataclass
class Incident:
    incident_id: str
    alert_ids: list[str]
    start: pd.Timestamp
    end: pd.Timestamp
    fault_id: str | None = None  # matched ground-truth fault, if any


@dataclass
class Decision:
    alert_id: str
    action: Action
    confidence: float
    reason: str
    incident_id: str | None = None
