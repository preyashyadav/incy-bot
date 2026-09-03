"""The `investigate` job: run the agent and record a proposal awaiting approval.

Idempotency: delivery is at-least-once, so this handler can run twice for one incident. Running
the agent again is wasteful but safe — the second proposal supersedes the first via
`create_proposal`, and neither can be executed without a fresh approval token. The guard below
short-circuits the common case so a redelivery does not pay for a second model call.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from incident_copilot.agent.runner import AgentError, run_agent
from incident_copilot.agent.schemas import InvalidProposedAction
from incident_copilot.config import get_settings
from incident_copilot.controlplane.pg_store import PostgresControlPlaneStore
from incident_copilot.controlplane.scenarios import get_registry
from incident_copilot.db import repositories as repo
from incident_copilot.db.models import EventType, Incident, Job, Proposal, ProposalStatus
from incident_copilot.jobs import handlers

logger = logging.getLogger(__name__)


@handlers.register("investigate")
def handle_investigate(session: Session, job: Job) -> None:
    incident = session.get(Incident, job.incident_id) if job.incident_id else None
    if incident is None:
        raise ValueError(f"job {job.id} has no resolvable incident")

    existing = session.execute(
        select(Proposal).where(
            Proposal.incident_id == incident.id, Proposal.status == ProposalStatus.PENDING
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("incident %s already has a pending proposal; skipping", incident.key)
        return

    settings = get_settings()
    scenario = get_registry().get(incident.scenario_key)
    state = PostgresControlPlaneStore().get_in(session, incident.scenario_key)

    repo.append_event(session, incident, EventType.INVESTIGATION_STARTED)

    try:
        proposal, result = run_agent(session, scenario, state)
    except AgentError as exc:
        # A failed investigation is reportable, not silent. The event goes on the timeline so the
        # Slack thread can say what happened and the incident lands in needs_attention.
        repo.append_event(
            session, incident, EventType.ERROR, {"stage": "investigate", "error": str(exc)}
        )
        raise

    repo.append_event(
        session,
        incident,
        EventType.EVIDENCE_GATHERED,
        {
            "tool_calls": result.tool_calls,
            "transcript": result.transcript,
            "cited_chunks": result.cited_chunks,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        },
    )

    # Validate the actions before the proposal is recorded, so a card is never rendered with a
    # button that cannot execute.
    try:
        validated = proposal.validated_actions()
    except InvalidProposedAction as exc:
        repo.append_event(
            session, incident, EventType.ERROR, {"stage": "propose", "error": str(exc)}
        )
        raise AgentError(f"proposed action was not executable: {exc}") from exc

    if proposal.severity != incident.severity:
        # The alert's severity is a first guess; the agent has evidence the alert did not.
        logger.info(
            "incident %s reclassified %s -> %s", incident.key, incident.severity, proposal.severity
        )
        repo.append_event(
            session,
            incident,
            EventType.NOTE_ADDED,
            {"note": "severity_reclassified", "from": incident.severity, "to": proposal.severity},
        )
        incident.severity = proposal.severity

    incident.summary = proposal.summary

    record = repo.create_proposal(
        session,
        incident,
        severity=proposal.severity,
        hypothesis=proposal.hypothesis,
        confidence=proposal.confidence,
        actions=[action.model_dump(mode="json") for action in proposal.actions],
        evidence_cited=proposal.evidence_cited,
        similar_incidents=proposal.similar_incidents,
        verification_plan=proposal.verification_plan,
        next_update_minutes=proposal.next_update_minutes,
        raw=proposal.model_dump(mode="json"),
    )

    # Mint the tokens the Slack card's buttons will carry. Phase 5 renders them; issuing them
    # here keeps the proposal and its authorisations in one transaction.
    repo.issue_approval_token(
        session, record, decision="approve", ttl_seconds=settings.approval_token_ttl_seconds
    )
    repo.issue_approval_token(
        session, record, decision="reject", ttl_seconds=settings.approval_token_ttl_seconds
    )

    logger.info(
        "incident %s: %s proposed with %s action(s), confidence %s",
        incident.key,
        proposal.severity,
        len(validated),
        proposal.confidence,
    )
