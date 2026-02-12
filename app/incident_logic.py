from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel

IncidentType = Literal["payments_failing", "login_outage", "latency_regression"]
SignalType = Literal["error_rate_spike", "availability_drop", "p95_latency_spike"]
Severity = Literal["SEV1", "SEV2", "SEV3"]


class AlertPayload(BaseModel):
    incident_type: IncidentType
    service: str
    signal: SignalType
    start_time: str
    impact: str
    region: str | None = None


class Assignee(BaseModel):
    team: str
    role: Literal["Primary", "Secondary"]


def classify_severity(alert: AlertPayload) -> Severity:
    if alert.incident_type in ("payments_failing", "login_outage"):
        return "SEV1"
    if alert.incident_type == "latency_regression":
        return "SEV2"
    return "SEV3"


def default_assignees(alert: AlertPayload) -> List[Assignee]:
    if alert.incident_type == "payments_failing":
        return [
            Assignee(team="Backend Oncall", role="Primary"),
            Assignee(team="Payments Team", role="Secondary"),
        ]
    if alert.incident_type == "login_outage":
        return [
            Assignee(team="Backend Oncall", role="Primary"),
            Assignee(team="Identity/Auth Team", role="Secondary"),
        ]
    return [
        Assignee(team="Backend Oncall", role="Primary"),
        Assignee(team="Performance/Infra Team", role="Secondary"),
    ]
