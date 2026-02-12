from __future__ import annotations

from typing import Optional

from sqlmodel import SQLModel, Field as SQLField


class Incident(SQLModel, table=True):
    incident_id: str = SQLField(primary_key=True, index=True)
    incident_type: str
    service: str
    signal: str
    start_time: str
    impact: str
    region: Optional[str] = None
    severity: str
    created_at: str
    assignees_json: str  # store list[assignee] as JSON string


class IncidentNote(SQLModel, table=True):
    note_id: str = SQLField(primary_key=True, index=True)
    incident_id: str = SQLField(index=True)
    created_at: str
    type: str
    title: Optional[str] = None
    payload_json: str  # store payload dict as JSON string
    created_by: Optional[str] = "orchestrate"
