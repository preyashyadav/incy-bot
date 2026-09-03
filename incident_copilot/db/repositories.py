"""Data access for incidents, their timeline, proposals, and executions.

The one rule enforced here: **status changes go through `append_event`.** Writing
`incident.status = ...` directly would leave the timeline and the projection disagreeing, and the
timeline is the thing that is auditable.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from incident_copilot.db.models import (
    ActionExecution,
    ApprovalToken,
    EventType,
    ExecutionStatus,
    Incident,
    IncidentEvent,
    IncidentStatus,
    Proposal,
    ProposalStatus,
    SlackDelivery,
)

# Which event advances an incident to which status. Events not listed here (notes, evidence,
# errors) are recorded without moving the incident.
_STATUS_FOR_EVENT: dict[EventType, IncidentStatus] = {
    EventType.CREATED: IncidentStatus.OPEN,
    EventType.INVESTIGATION_STARTED: IncidentStatus.INVESTIGATING,
    EventType.PROPOSAL_CREATED: IncidentStatus.AWAITING_APPROVAL,
    EventType.APPROVED: IncidentStatus.REMEDIATING,
    EventType.REJECTED: IncidentStatus.NEEDS_ATTENTION,
    EventType.ACTION_FAILED: IncidentStatus.NEEDS_ATTENTION,
    EventType.VERIFIED: IncidentStatus.RESOLVED,
    EventType.VERIFICATION_FAILED: IncidentStatus.NEEDS_ATTENTION,
    EventType.RESOLVED: IncidentStatus.RESOLVED,
}


def _now() -> datetime:
    return datetime.now(UTC)


def new_incident_key() -> str:
    return f"INC-{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# Incidents and their timeline
# ---------------------------------------------------------------------------


def create_incident(
    session: Session,
    *,
    scenario_key: str,
    service: str,
    severity: str,
    title: str,
    region: str | None = None,
    slack_channel_id: str | None = None,
    slack_thread_ts: str | None = None,
) -> Incident:
    incident = Incident(
        key=new_incident_key(),
        scenario_key=scenario_key,
        service=service,
        region=region,
        severity=severity,
        title=title,
        status=IncidentStatus.OPEN,
        slack_channel_id=slack_channel_id,
        slack_thread_ts=slack_thread_ts,
    )
    session.add(incident)
    session.flush()
    append_event(session, incident, EventType.CREATED, {"scenario": scenario_key})
    return incident


def append_event(
    session: Session,
    incident: Incident,
    event_type: EventType,
    payload: dict[str, Any] | None = None,
    *,
    actor: str = "system",
) -> IncidentEvent:
    """Append to the timeline and advance the incident's projected status.

    `seq` is allocated as MAX(seq)+1, which two concurrent appends can compute identically. The
    unique constraint on (incident_id, seq) turns that race into an IntegrityError rather than a
    silent duplicate, and we retry with a fresh sequence. Bounded, because unbounded retries on a
    hot incident would be a worse failure than raising.
    """
    for _attempt in range(5):
        next_seq = session.execute(
            select(func.coalesce(func.max(IncidentEvent.seq), 0) + 1).where(
                IncidentEvent.incident_id == incident.id
            )
        ).scalar_one()
        event = IncidentEvent(
            incident_id=incident.id,
            seq=next_seq,
            type=event_type,
            payload=payload or {},
            actor=actor,
        )
        try:
            with session.begin_nested():
                session.add(event)
                session.flush()
            break
        except IntegrityError:
            continue
    else:
        raise RuntimeError(
            f"could not allocate an event sequence for {incident.key} after 5 attempts"
        )

    new_status = _STATUS_FOR_EVENT.get(event_type)
    if new_status is not None:
        incident.status = new_status
        if new_status == IncidentStatus.RESOLVED and incident.resolved_at is None:
            incident.resolved_at = _now()
    session.flush()
    return event


def get_incident_by_key(session: Session, key: str) -> Incident | None:
    return session.execute(select(Incident).where(Incident.key == key)).scalar_one_or_none()


def get_incident_by_thread(session: Session, channel_id: str, thread_ts: str) -> Incident | None:
    """Resolve the incident a Slack thread belongs to — how replies find their context."""
    return session.execute(
        select(Incident).where(
            Incident.slack_channel_id == channel_id, Incident.slack_thread_ts == thread_ts
        )
    ).scalar_one_or_none()


def timeline(session: Session, incident: Incident) -> list[IncidentEvent]:
    return list(
        session.execute(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident.id)
            .order_by(IncidentEvent.seq)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Proposals and approvals
# ---------------------------------------------------------------------------


def create_proposal(
    session: Session,
    incident: Incident,
    *,
    severity: str,
    hypothesis: str,
    confidence: str,
    actions: list[dict[str, Any]],
    evidence_cited: list[str] | None = None,
    similar_incidents: list[str] | None = None,
    verification_plan: str | None = None,
    next_update_minutes: int = 15,
    raw: dict[str, Any] | None = None,
) -> Proposal:
    """Record a proposal, superseding any that is still pending.

    Only one proposal per incident may be actionable at a time. Without this, a re-proposal
    after a failed verification would leave the earlier card's buttons live, and approving the
    stale one would apply a remediation the agent has already moved on from.
    """
    for stale in session.execute(
        select(Proposal).where(
            Proposal.incident_id == incident.id, Proposal.status == ProposalStatus.PENDING
        )
    ).scalars():
        stale.status = ProposalStatus.SUPERSEDED

    proposal = Proposal(
        incident_id=incident.id,
        severity=severity,
        hypothesis=hypothesis,
        confidence=confidence,
        actions=actions,
        evidence_cited=evidence_cited or [],
        similar_incidents=similar_incidents or [],
        verification_plan=verification_plan,
        next_update_minutes=next_update_minutes,
        raw=raw or {},
    )
    session.add(proposal)
    session.flush()
    append_event(
        session,
        incident,
        EventType.PROPOSAL_CREATED,
        {"proposal_id": str(proposal.id), "actions": len(actions)},
    )
    return proposal


def issue_approval_token(
    session: Session,
    proposal: Proposal,
    *,
    decision: str,
    ttl_seconds: int,
    action_index: int | None = None,
) -> ApprovalToken:
    """Mint an opaque single-use token for one Slack button."""
    token = ApprovalToken(
        token=secrets.token_urlsafe(32),
        proposal_id=proposal.id,
        decision=decision,
        action_index=action_index,
        expires_at=_now() + timedelta(seconds=ttl_seconds),
    )
    session.add(token)
    session.flush()
    return token


class TokenRejected(Exception):
    """A token was missing, expired, or already used."""


def consume_approval_token(session: Session, token_value: str, *, actor: str) -> ApprovalToken:
    """Atomically redeem a token, or refuse.

    The row is locked before the consumed check so two simultaneous clicks cannot both pass it.
    Refusal reasons are deliberately not distinguished to the caller beyond this message — an
    unauthenticated probe should not learn whether a token exists.
    """
    token = session.execute(
        select(ApprovalToken).where(ApprovalToken.token == token_value).with_for_update()
    ).scalar_one_or_none()

    if token is None:
        raise TokenRejected("this approval is no longer valid")
    if token.consumed_at is not None:
        raise TokenRejected("this approval has already been used")
    if token.expires_at <= _now():
        raise TokenRejected("this approval has expired")

    token.consumed_at = _now()
    token.consumed_by = actor
    session.flush()
    return token


def decide_proposal(
    session: Session, proposal: Proposal, *, approved: bool, actor: str
) -> Proposal:
    proposal.status = ProposalStatus.APPROVED if approved else ProposalStatus.REJECTED
    proposal.decided_at = _now()
    proposal.decided_by = actor
    session.flush()

    incident = session.get(Incident, proposal.incident_id)
    assert incident is not None
    append_event(
        session,
        incident,
        EventType.APPROVED if approved else EventType.REJECTED,
        {"proposal_id": str(proposal.id)},
        actor=actor,
    )
    return proposal


def expire_stale_proposals(session: Session) -> int:
    """Mark pending proposals whose tokens have all expired.

    A proposal nobody acted on should not sit as `pending` forever — it keeps the incident in
    `awaiting_approval` and makes the queue look busier than it is.
    """
    stale = session.execute(
        select(Proposal)
        .join(ApprovalToken, ApprovalToken.proposal_id == Proposal.id)
        .where(Proposal.status == ProposalStatus.PENDING)
        .group_by(Proposal.id)
        .having(func.max(ApprovalToken.expires_at) <= _now())
    ).scalars()

    count = 0
    for proposal in stale:
        proposal.status = ProposalStatus.EXPIRED
        count += 1
    session.flush()
    return count


# ---------------------------------------------------------------------------
# Executions
# ---------------------------------------------------------------------------


def record_execution(
    session: Session,
    incident: Incident,
    proposal: Proposal,
    *,
    action_index: int,
    kind: str,
    params: dict[str, Any],
) -> ActionExecution:
    execution = ActionExecution(
        incident_id=incident.id,
        proposal_id=proposal.id,
        action_index=action_index,
        kind=kind,
        params=params,
        status=ExecutionStatus.PENDING,
    )
    session.add(execution)
    session.flush()
    return execution


def finish_execution(
    session: Session,
    execution: ActionExecution,
    *,
    status: ExecutionStatus,
    summary: str | None = None,
    error: str | None = None,
    state_before: dict[str, Any] | None = None,
    state_after: dict[str, Any] | None = None,
) -> ActionExecution:
    execution.status = status
    execution.summary = summary
    execution.error = error
    execution.state_before = state_before
    execution.state_after = state_after
    execution.finished_at = _now()
    session.flush()

    incident = session.get(Incident, execution.incident_id)
    assert incident is not None
    append_event(
        session,
        incident,
        EventType.ACTION_EXECUTED
        if status == ExecutionStatus.SUCCEEDED
        else EventType.ACTION_FAILED,
        {
            "execution_id": str(execution.id),
            "kind": execution.kind,
            "summary": summary,
            "error": error,
        },
    )
    return execution


# ---------------------------------------------------------------------------
# Slack delivery de-duplication
# ---------------------------------------------------------------------------


def is_duplicate_delivery(session: Session, delivery_key: str, kind: str) -> bool:
    """Record a Slack delivery; return True if it has been seen before.

    Insert-and-catch rather than check-then-insert: two concurrent redeliveries of the same
    interaction would both pass a SELECT, and only the unique constraint can actually settle it.
    """
    try:
        with session.begin_nested():
            session.add(SlackDelivery(delivery_key=delivery_key, kind=kind))
            session.flush()
    except IntegrityError:
        return True
    return False
