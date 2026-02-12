
from __future__ import annotations
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")


import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import SQLModel, Session, select

from app.db import engine
from app.incident_logic import AlertPayload, Assignee, IncidentType, Severity, classify_severity, default_assignees
from app.kb import init_kb, seed_kb_if_empty, kb_search
from app.models import Incident, IncidentNote

from app.slack import router as slack_router

from app.incident_api import router as incident_router

from app.approvals_api import router as approvals_router

# ----------------------------
# Request/Response Models
# ----------------------------

class CreateIncidentResponse(BaseModel):
    incident_id: str
    severity: Severity
    created_at: str


class AssignResponse(BaseModel):
    incident_id: str
    assignees: List[Assignee]


class EvidenceBundle(BaseModel):
    logs: Dict[str, Any]
    metrics: Dict[str, Any]
    changes: Dict[str, Any]
    runbook: Dict[str, Any]


class EvidenceResponse(BaseModel):
    incident_id: str
    incident_type: IncidentType
    service: str
    region: Optional[str] = None
    evidence_bundle: EvidenceBundle


class AddNoteRequest(BaseModel):
    type: str = Field(..., description="Note type, e.g., comms_postmortem")
    title: Optional[str] = Field(None, description="Short title for the note")
    payload: Dict[str, Any] = Field(..., description="Structured content to store (JSON)")
    created_by: Optional[str] = Field("orchestrate", description="Source of the note")


class AddNoteResponse(BaseModel):
    ok: bool
    incident_id: str
    note_id: str
    created_at: str
    notes_count: int


class KBSearchResponse(BaseModel):
    query: str
    matched_query: str
    top_k: int
    results: List[Dict[str, Any]]



# ----------------------------
# App + DB init
# ----------------------------

app = FastAPI(
    title="Incident Evidence Service",
    version="0.2.0",
    description="Mock evidence + incident state + KB retrieval for Incident Response demo (hackathon POC).",
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

app.include_router(slack_router)
app.include_router(incident_router)
app.include_router(approvals_router)

@app.on_event("startup")
def on_startup() -> None:
    # Create incident + notes tables
    SQLModel.metadata.create_all(engine)

    # Init + seed knowledge base chunks (FTS5) in same incidents.db
    init_kb()
    seed_kb_if_empty()


# ----------------------------
# Helpers
# ----------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_fixture(incident_type: str, name: str) -> Dict[str, Any]:
    path = FIXTURES_DIR / incident_type / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing fixture: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_incident_or_404(session: Session, incident_id: str) -> Incident:
    inc = session.exec(select(Incident).where(Incident.incident_id == incident_id)).first()
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")
    return inc


# ----------------------------
# Endpoints (Tools)
# ----------------------------

@app.post("/incidents", response_model=CreateIncidentResponse)
def create_incident(alert: AlertPayload) -> CreateIncidentResponse:
    incident_id = f"INC-{uuid4().hex[:8].upper()}"
    severity = classify_severity(alert)
    created_at = now_iso()
    assignees = [a.model_dump() for a in default_assignees(alert)]

    inc_row = Incident(
        incident_id=incident_id,
        incident_type=alert.incident_type,
        service=alert.service,
        signal=alert.signal,
        start_time=alert.start_time,
        impact=alert.impact,
        region=alert.region,
        severity=severity,
        created_at=created_at,
        assignees_json=json.dumps(assignees),
    )

    with Session(engine) as session:
        session.add(inc_row)
        session.commit()

    return CreateIncidentResponse(incident_id=incident_id, severity=severity, created_at=created_at)


@app.post("/incidents/{incident_id}/assign", response_model=AssignResponse)
def assign_owners(incident_id: str) -> AssignResponse:
    with Session(engine) as session:
        inc = get_incident_or_404(session, incident_id)
        assignees_raw = json.loads(inc.assignees_json)
        assignees = [Assignee(**a) for a in assignees_raw]
        return AssignResponse(incident_id=incident_id, assignees=assignees)


@app.get("/incidents/{incident_id}/evidence", response_model=EvidenceResponse)
def get_evidence(incident_id: str) -> EvidenceResponse:
    with Session(engine) as session:
        inc = get_incident_or_404(session, incident_id)

    itype = inc.incident_type
    try:
        logs = load_fixture(itype, "logs")
        metrics = load_fixture(itype, "metrics")
        changes = load_fixture(itype, "changes")
        runbook = load_fixture(itype, "runbook")
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return EvidenceResponse(
        incident_id=inc.incident_id,
        incident_type=itype,  # type: ignore
        service=inc.service,
        region=inc.region,
        evidence_bundle=EvidenceBundle(
            logs=logs,
            metrics=metrics,
            changes=changes,
            runbook=runbook,
        ),
    )


@app.post("/incidents/{incident_id}/notes", response_model=AddNoteResponse)
def add_note(incident_id: str, note: AddNoteRequest) -> AddNoteResponse:
    with Session(engine) as session:
        _ = get_incident_or_404(session, incident_id)

        note_id = f"NOTE-{uuid4().hex[:8].upper()}"
        created_at = now_iso()

        row = IncidentNote(
            note_id=note_id,
            incident_id=incident_id,
            created_at=created_at,
            type=note.type,
            title=note.title,
            payload_json=json.dumps(note.payload),
            created_by=note.created_by,
        )
        session.add(row)
        session.commit()

        # count notes for this incident (for demo)
        notes_count = len(
            session.exec(select(IncidentNote).where(IncidentNote.incident_id == incident_id)).all()
        )

        return AddNoteResponse(
            ok=True,
            incident_id=incident_id,
            note_id=note_id,
            created_at=created_at,
            notes_count=notes_count,
        )


def normalize_fts_token(s: str) -> str:
    # FTS5-safe normalization
    return s.replace("-", "_").lower()

@app.get("/kb/search", response_model=KBSearchResponse)
def search_kb(
    q: str = Query(..., description="Search query for runbooks/policies"),
    k: int = Query(3, ge=1, le=10, description="Number of results"),
    incident_type: Optional[str] = Query(None, description="Optional incident type (boost)"),
    service: Optional[str] = Query(None, description="Optional service (boost)"),
) -> KBSearchResponse:

    # Normalize tags for FTS safety (IMPORTANT)
    tags = " ".join(
        normalize_fts_token(t)
        for t in [incident_type, service]
        if t
    ).strip()

    q_raw = q.strip()
    q_lower = q_raw.lower()

    # Debug (optional, keep for demo)
    print("KB_SEARCH raw q =", repr(q_raw))

    sev_tokens = {"sev1", "sev2", "sev3"}

    # Rewrite severity queries for higher recall
    if (
        any(tok in q_lower for tok in sev_tokens)
        or "severity" in q_lower
        or "rubric" in q_lower
    ):
        mentioned = [tok for tok in sev_tokens if tok in q_lower]
        base_terms = ["sev", "severity", "rubric", "policy"] + mentioned

        # FTS5 OR query
        q_norm = " OR ".join(base_terms)
    else:
        q_norm = q_raw

    # Extra safety: normalize hyphens in query too
    q_norm = normalize_fts_token(q_norm)

    print("KB_SEARCH normalized =", repr(q_norm), "tags =", repr(tags))

    results = kb_search(
        q=q_norm,
        k=k,
        tags=tags if tags else None
    )

    return KBSearchResponse(
        query=q,
        matched_query=q_norm,
        top_k=k,
        results=results,
    )





