"""Incident operations behind the Slack handlers.

Deliberately Slack-free: these functions take plain arguments and return plain results, so the
whole approval flow can be tested without a workspace, a signature, or a mocked WebClient. The
handlers in `handlers.py` are then thin — parse, call, reply — which is where handler logic
belongs, because everything above the parse is the part worth testing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from incident_copilot.config import get_settings
from incident_copilot.controlplane.pg_store import PostgresControlPlaneStore
from incident_copilot.controlplane.scenarios import Scenario, ScenarioError, get_registry
from incident_copilot.db import repositories as repo
from incident_copilot.db.models import (
    ApprovalToken,
    EventType,
    Incident,
    Proposal,
    ProposalStatus,
)
from incident_copilot.jobs import queue

logger = logging.getLogger(__name__)


class SlackActionRefused(Exception):
    """The requested action cannot be performed. The message is shown to the user verbatim."""


@dataclass
class TriagedIncident:
    incident: Incident
    scenario: Scenario
    investigate_token: ApprovalToken


def triage_scenario(
    session: Session, scenario_key: str, *, channel_id: str, actor: str
) -> TriagedIncident:
    """Open an incident for a scenario and reset that scenario's world.

    The reset is what makes the demo repeatable: running `/incident triage` twice gives the
    second run the same starting conditions as the first.
    """
    try:
        scenario = get_registry().get(scenario_key)
    except ScenarioError as exc:
        raise SlackActionRefused(str(exc)) from exc

    PostgresControlPlaneStore().reset_in(session, scenario.key)

    incident = repo.create_incident(
        session,
        scenario_key=scenario.key,
        service=scenario.service,
        severity=scenario.expected_severity,
        title=scenario.alert.summary,
        region=scenario.initial.region,
        slack_channel_id=channel_id,
    )
    # The Investigate button is gated by a token like every other action, so a stale alert card
    # cannot start an investigation after the incident has moved on.
    token = repo.issue_approval_token(
        session,
        # No proposal exists yet, so this token is attached once one does; until then it is a
        # bare authorisation to start work on this incident.
        _placeholder_proposal(session, incident),
        decision="approve",
        ttl_seconds=get_settings().approval_token_ttl_seconds,
    )
    return TriagedIncident(incident=incident, scenario=scenario, investigate_token=token)


def _placeholder_proposal(session: Session, incident: Incident) -> Proposal:
    """A proposal row representing 'investigate this incident'.

    Approval tokens hang off proposals, and the Investigate button needs the same single-use,
    expiring guarantees as any other button. Reusing the proposal row rather than inventing a
    second token table keeps one redemption path — and therefore one place where replay is
    prevented — instead of two that must be kept in step.
    """
    return repo.create_proposal(
        session,
        incident,
        severity=incident.severity,
        hypothesis="Investigation not started.",
        confidence="low",
        actions=[],
        verification_plan="",
    )


def attach_thread(session: Session, incident: Incident, *, channel_id: str, ts: str) -> None:
    """Record where the incident lives in Slack, so later updates land in the same thread."""
    incident.slack_channel_id = channel_id
    incident.slack_thread_ts = ts
    session.flush()


def start_investigation(session: Session, token_value: str, *, actor: str) -> Incident:
    """Redeem the Investigate token and queue the agent."""
    token = _consume(session, token_value, actor=actor)
    proposal = session.get(Proposal, token.proposal_id)
    assert proposal is not None
    incident = session.get(Incident, proposal.incident_id)
    assert incident is not None

    # The placeholder is not a real proposal; retire it so the incident is not left looking as
    # though it is awaiting approval on an empty one.
    if not proposal.actions:
        proposal.status = ProposalStatus.SUPERSEDED

    queue.enqueue(
        session,
        "investigate",
        {},
        # One investigation per incident, however many times the button is clicked or Slack
        # redelivers the interaction.
        idem_key=f"investigate:{incident.id}",
        incident_id=incident.id,
    )
    session.flush()
    return incident


def ignore_alert(session: Session, token_value: str, *, actor: str) -> Incident:
    token = _consume(session, token_value, actor=actor)
    proposal = session.get(Proposal, token.proposal_id)
    assert proposal is not None
    incident = session.get(Incident, proposal.incident_id)
    assert incident is not None

    proposal.status = ProposalStatus.REJECTED
    repo.append_event(
        session, incident, EventType.NOTE_ADDED, {"note": "alert_ignored"}, actor=actor
    )
    session.flush()
    return incident


@dataclass
class Decision:
    incident: Incident
    proposal: Proposal
    approved: bool


def decide(session: Session, token_value: str, *, actor: str) -> Decision:
    """Redeem an approve/reject token and act on it.

    The token carries the decision, not the button — so a client that rewrites `action_id` still
    cannot turn a rejection into an approval.
    """
    token = _consume(session, token_value, actor=actor)
    proposal = session.get(Proposal, token.proposal_id)
    assert proposal is not None
    incident = session.get(Incident, proposal.incident_id)
    assert incident is not None

    if proposal.status is not ProposalStatus.PENDING:
        raise SlackActionRefused(
            f"That proposal is already {proposal.status.value} and cannot be changed."
        )

    approved = token.decision == "approve"
    repo.decide_proposal(session, proposal, approved=approved, actor=actor)

    if approved:
        queue.enqueue(
            session,
            "execute_remediation",
            {"proposal_id": str(proposal.id)},
            idem_key=f"execute:{proposal.id}",
            incident_id=incident.id,
        )
    session.flush()
    return Decision(incident=incident, proposal=proposal, approved=approved)


def _consume(session: Session, token_value: str | None, *, actor: str) -> ApprovalToken:
    if not token_value:
        raise SlackActionRefused("That button is missing its authorisation and cannot be used.")
    try:
        return repo.consume_approval_token(session, token_value, actor=actor)
    except repo.TokenRejected as exc:
        raise SlackActionRefused(str(exc)) from exc


# ---------------------------------------------------------------------------
# Read-only lookups
# ---------------------------------------------------------------------------


def latest_proposal(session: Session, incident: Incident) -> Proposal | None:
    proposals = [p for p in incident.proposals if p.actions]
    return proposals[-1] if proposals else None


def incident_for_thread(session: Session, channel_id: str, thread_ts: str) -> Incident | None:
    return repo.get_incident_by_thread(session, channel_id, thread_ts)


def find_incident(session: Session, key: str) -> Incident:
    incident = repo.get_incident_by_key(session, key.strip().upper())
    if incident is None:
        raise SlackActionRefused(f"No incident found with key `{key}`.")
    return incident


def scenario_catalogue() -> list[tuple[str, str, str]]:
    return [(s.key, s.title, s.expected_severity) for s in get_registry().all()]
