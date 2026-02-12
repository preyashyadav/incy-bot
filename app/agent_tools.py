from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlmodel import Session, select

from app.db import engine
from app.incident_logic import AlertPayload, Assignee, classify_severity, default_assignees
from app.kb import kb_search
from app.models import Incident, IncidentNote

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_fixture(incident_type: str, name: str) -> Dict[str, Any]:
    path = FIXTURES_DIR / incident_type / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing fixture: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def create_incident_tool(alert: Dict[str, Any]) -> Dict[str, Any]:
    allowed_incident_types = {"payments_failing", "login_outage", "latency_regression"}
    allowed_signals = {"error_rate_spike", "availability_drop", "p95_latency_spike"}

    incident_type = alert.get("incident_type")
    if incident_type not in allowed_incident_types:
        incident_type = "payments_failing"

    signal = alert.get("signal")
    if signal not in allowed_signals:
        signal = "error_rate_spike"

    normalized = {
        "incident_type": incident_type,
        "service": alert.get("service") or "unknown",
        "signal": signal,
        "start_time": alert.get("start_time") or alert.get("timestamp") or _now_iso(),
        "impact": alert.get("impact") or alert.get("short_summary") or "unknown impact",
        "region": alert.get("region"),
    }
    parsed = AlertPayload(**normalized)
    incident_id = f"INC-{uuid4().hex[:8].upper()}"
    severity = classify_severity(parsed)
    created_at = _now_iso()
    assignees = [a.model_dump() for a in default_assignees(parsed)]

    inc_row = Incident(
        incident_id=incident_id,
        incident_type=parsed.incident_type,
        service=parsed.service,
        signal=parsed.signal,
        start_time=parsed.start_time,
        impact=parsed.impact,
        region=parsed.region,
        severity=severity,
        created_at=created_at,
        assignees_json=json.dumps(assignees),
    )

    with Session(engine) as session:
        session.add(inc_row)
        session.commit()

    return {"incident_id": incident_id, "severity": severity, "created_at": created_at}


def assign_owners_tool(incident_id: str) -> Dict[str, Any]:
    with Session(engine) as session:
        inc = session.exec(select(Incident).where(Incident.incident_id == incident_id)).first()
        if not inc:
            return {"ok": False, "reason": "incident not found"}
        assignees_raw = json.loads(inc.assignees_json)
        assignees = [Assignee(**a).model_dump() for a in assignees_raw]
        return {"incident_id": incident_id, "assignees": assignees}


def get_evidence_tool(incident_id: str) -> Dict[str, Any]:
    with Session(engine) as session:
        inc = session.exec(select(Incident).where(Incident.incident_id == incident_id)).first()
        if not inc:
            return {"ok": False, "reason": "incident not found"}

    itype = inc.incident_type
    try:
        logs = _load_fixture(itype, "logs")
        metrics = _load_fixture(itype, "metrics")
        changes = _load_fixture(itype, "changes")
        runbook = _load_fixture(itype, "runbook")
    except FileNotFoundError as e:
        return {"ok": False, "reason": str(e)}

    return {
        "incident_id": inc.incident_id,
        "incident_type": itype,
        "service": inc.service,
        "region": inc.region,
        "evidence_bundle": {
            "logs": logs,
            "metrics": metrics,
            "changes": changes,
            "runbook": runbook,
        },
    }


def add_note_tool(
    incident_id: str,
    note_type: str,
    payload: Dict[str, Any],
    title: Optional[str] = None,
    created_by: Optional[str] = "orchestrate",
) -> Dict[str, Any]:
    with Session(engine) as session:
        inc = session.exec(select(Incident).where(Incident.incident_id == incident_id)).first()
        if not inc:
            return {"ok": False, "reason": "incident not found"}

        note_id = f"NOTE-{uuid4().hex[:8].upper()}"
        created_at = _now_iso()

        row = IncidentNote(
            note_id=note_id,
            incident_id=incident_id,
            created_at=created_at,
            type=note_type,
            title=title,
            payload_json=json.dumps(payload),
            created_by=created_by,
        )
        session.add(row)
        session.commit()

        notes_count = len(
            session.exec(select(IncidentNote).where(IncidentNote.incident_id == incident_id)).all()
        )

    return {
        "ok": True,
        "incident_id": incident_id,
        "note_id": note_id,
        "created_at": created_at,
        "notes_count": notes_count,
    }


def search_kb_tool(
    query: str,
    k: int = 3,
    incident_type: Optional[str] = None,
    service: Optional[str] = None,
) -> Dict[str, Any]:
    results = kb_search(q=query, k=k, tags=" ".join([t for t in [incident_type, service] if t]) or None)
    return {"query": query, "top_k": k, "results": results}


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_incident",
            "description": "Create an incident record from an alert payload.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alert": {
                        "type": "object",
                        "properties": {
                            "incident_type": {"type": "string"},
                            "service": {"type": "string"},
                            "signal": {"type": "string"},
                            "start_time": {"type": "string"},
                            "impact": {"type": "string"},
                            "region": {"type": ["string", "null"]},
                        },
                    }
                },
                "required": ["alert"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_owners",
            "description": "Fetch default assignees for an incident.",
            "parameters": {
                "type": "object",
                "properties": {"incident_id": {"type": "string"}},
                "required": ["incident_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_evidence",
            "description": "Get evidence bundle (logs/metrics/changes/runbook) for an incident.",
            "parameters": {
                "type": "object",
                "properties": {"incident_id": {"type": "string"}},
                "required": ["incident_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_search",
            "description": "Search the knowledge base for relevant runbooks and policies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "minimum": 1, "maximum": 10},
                    "incident_type": {"type": ["string", "null"]},
                    "service": {"type": ["string", "null"]},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": "Attach a structured note to an incident.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "note_type": {"type": "string"},
                    "title": {"type": ["string", "null"]},
                    "payload": {"type": "object"},
                    "created_by": {"type": ["string", "null"]},
                },
                "required": ["incident_id", "note_type", "payload"],
            },
        },
    },
]


def dispatch_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "create_incident":
        return create_incident_tool(args["alert"])
    if name == "assign_owners":
        return assign_owners_tool(args["incident_id"])
    if name == "get_evidence":
        return get_evidence_tool(args["incident_id"])
    if name == "kb_search":
        return search_kb_tool(
            query=args["query"],
            k=int(args.get("k", 3)),
            incident_type=args.get("incident_type"),
            service=args.get("service"),
        )
    if name == "add_note":
        return add_note_tool(
            incident_id=args["incident_id"],
            note_type=args["note_type"],
            title=args.get("title"),
            payload=args.get("payload", {}),
            created_by=args.get("created_by"),
        )
    return {"ok": False, "reason": f"unknown tool '{name}'"}
