"""A read-only dashboard over incidents, proposals, and the knowledge base.

Server-rendered HTML, no build step and no JavaScript: this is an operations view read a handful
of times per incident, and a bundler would be more machinery than the feature is worth.

The point of the thing is **provenance**. Slack shows a proposal and a decision; this shows what
the proposal was built from — which tools ran, which documents were retrieved, and what each
claim was said to rest on — with every retrieved chunk linking to its own text and back to the
incidents that cited it. A hypothesis you can trace is worth considerably more than one you have
to take on faith.

Strictly read-only. Nothing here changes state; the approval path stays in Slack, where the
tokens are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from incident_copilot.controlplane.pg_store import PostgresControlPlaneStore
from incident_copilot.controlplane.scenarios import ScenarioError, get_registry
from incident_copilot.controlplane.simulator import assess_health, compute_metrics
from incident_copilot.db import repositories as repo
from incident_copilot.db.models import ActionExecution, Incident, IncidentStatus, KBChunk, Proposal
from incident_copilot.db.session import session_scope
from incident_copilot.slack import blocks as B

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_session() -> Any:
    with session_scope() as session:
        yield session


SessionDep = Depends(get_session)


def _chunk_link(identifier: str) -> dict[str, Any]:
    """Render one citation.

    Evidence citations mix two kinds of thing: tool names (`get_metrics`) and retrieved chunk ids
    (`kb:runbook-payments-gateway:mitigation`). Only the latter has somewhere to point, so tool
    names render as inert chips rather than dead links.
    """
    if identifier.startswith(("kb:", "history:")):
        return {"label": identifier, "url": f"/dashboard/kb/{identifier}"}
    return {"label": identifier, "url": None}


def _event_detail(event: Any) -> str:
    """A one-line summary of an event's payload, for the timeline."""
    payload = event.payload or {}
    match event.type.value:
        case "evidence_gathered":
            return (
                f"{payload.get('tool_calls', 0)} tool calls, "
                f"{len(payload.get('cited_chunks', []))} documents"
            )
        case "proposal_created":
            return f"{payload.get('actions', 0)} action(s) proposed"
        case "note_added" if payload.get("note") == "severity_reclassified":
            return f"{payload.get('from')} → {payload.get('to')}"
        case "note_added":
            return str(payload.get("note", ""))
        case "action_executed" | "action_failed":
            return str(payload.get("summary") or payload.get("error") or "")
        case "verified" | "verification_failed":
            return str(payload.get("detail", ""))
        case "error":
            return f"{payload.get('stage', '')}: {payload.get('error', '')}"
        case _:
            return ""


def _latest_real_proposal(incident: Incident) -> Proposal | None:
    """The newest proposal that actually proposes something.

    Skips the placeholder row created at triage time to hang the Investigate token off.
    """
    real = [p for p in incident.proposals if p.actions]
    return real[-1] if real else None


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def incident_list(request: Request, session: Session = SessionDep) -> HTMLResponse:
    incidents = list(
        session.execute(select(Incident).order_by(Incident.opened_at.desc())).scalars()
    )
    rows = []
    for incident in incidents:
        proposal = _latest_real_proposal(incident)
        rows.append(
            {
                "incident": incident,
                "actions": [a.get("kind", "?") for a in proposal.actions] if proposal else [],
            }
        )
    return templates.TemplateResponse(
        request,
        "incidents.html",
        {
            "nav": "incidents",
            "incidents": rows,
            "resolved": sum(i.status is IncidentStatus.RESOLVED for i in incidents),
            "awaiting": sum(i.status is IncidentStatus.AWAITING_APPROVAL for i in incidents),
        },
    )


@router.get("/incidents/{key}", response_class=HTMLResponse)
def incident_detail(key: str, request: Request, session: Session = SessionDep) -> HTMLResponse:
    incident = repo.get_incident_by_key(session, key.upper())
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no incident {key}")

    events = repo.timeline(session, incident)
    proposal = _latest_real_proposal(incident)

    evidence = next(
        (e.payload for e in reversed(events) if e.type.value == "evidence_gathered"), {}
    )

    actions: list[dict[str, Any]] = []
    if proposal is not None:
        for raw in proposal.actions:
            actions.append(
                {
                    "describe": B.describe_action(raw).replace("*", "").replace("`", ""),
                    "risk": raw.get("risk", "unknown"),
                    "reversible": bool(raw.get("reversible")),
                    "rationale": raw.get("rationale", ""),
                }
            )

    # Current derived state for the incident's scenario, so the page shows whether the system is
    # still broken rather than only what was said about it.
    metrics = health = None
    try:
        scenario = get_registry().get(incident.scenario_key)
        state = PostgresControlPlaneStore().get_in(session, incident.scenario_key)
        metrics = compute_metrics(scenario, state)
        health = assess_health(scenario, state)
    except ScenarioError:
        pass

    executions = list(
        session.execute(
            select(ActionExecution)
            .where(ActionExecution.incident_id == incident.id)
            .order_by(ActionExecution.action_index)
        ).scalars()
    )

    return templates.TemplateResponse(
        request,
        "incident.html",
        {
            "nav": "incidents",
            "incident": incident,
            "proposal": proposal,
            "actions": actions,
            "ruled_out": (proposal.raw or {}).get("ruled_out", []) if proposal else [],
            "citations": [_chunk_link(c) for c in (proposal.evidence_cited if proposal else [])],
            "retrieved": [_chunk_link(c) for c in evidence.get("cited_chunks", [])],
            "transcript": evidence.get("transcript", []),
            "usage": evidence or None,
            "events": [
                type(
                    "E",
                    (),
                    {"created_at": e.created_at, "type": e.type, "detail": _event_detail(e)},
                )
                for e in events
            ],
            "executions": executions,
            "metrics": metrics,
            "health": health,
        },
    )


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------


@router.get("/kb", response_class=HTMLResponse)
def kb_index(request: Request, session: Session = SessionDep) -> HTMLResponse:
    chunks = list(session.execute(select(KBChunk).order_by(KBChunk.chunk_id)).scalars())
    return templates.TemplateResponse(
        request,
        "kb.html",
        {
            "nav": "kb",
            "kb": [c for c in chunks if c.corpus == "kb"],
            "history": [c for c in chunks if c.corpus == "history"],
        },
    )


@router.get("/kb/{chunk_id:path}", response_class=HTMLResponse)
def kb_chunk(chunk_id: str, request: Request, session: Session = SessionDep) -> HTMLResponse:
    chunk = session.execute(
        select(KBChunk).where(KBChunk.chunk_id == chunk_id)
    ).scalar_one_or_none()
    if chunk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no chunk {chunk_id}")

    # The reverse index: which incidents leaned on this document. Cheap at this corpus size, and
    # it turns the knowledge base from a static pile into something with a usage record.
    cited_by = [
        incident
        for incident in session.execute(select(Incident)).scalars()
        if any(
            chunk_id in (p.evidence_cited or []) or chunk_id in _retrieved_chunks(session, incident)
            for p in incident.proposals
        )
    ]

    return templates.TemplateResponse(
        request, "chunk.html", {"nav": "kb", "chunk": chunk, "cited_by": cited_by}
    )


def _retrieved_chunks(session: Session, incident: Incident) -> list[str]:
    for event in reversed(repo.timeline(session, incident)):
        if event.type.value == "evidence_gathered":
            return list(event.payload.get("cited_chunks", []))
    return []


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@router.get("/scenarios", response_class=HTMLResponse)
def scenario_list(request: Request, session: Session = SessionDep) -> HTMLResponse:
    store = PostgresControlPlaneStore()
    rows = []
    for scenario in get_registry().all():
        state = store.get_in(session, scenario.key)
        rows.append(
            {
                "key": scenario.key,
                "title": scenario.title,
                "description": " ".join(scenario.description.split()),
                "expected_severity": scenario.expected_severity,
                "metrics": compute_metrics(scenario, state),
                "healthy": assess_health(scenario, state).healthy,
            }
        )
    return templates.TemplateResponse(
        request, "scenarios.html", {"nav": "scenarios", "scenarios": rows}
    )
