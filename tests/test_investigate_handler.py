"""The `investigate` job handler: agent output becomes a persisted, approvable proposal."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session

from incident_copilot.agent.runner import AgentError
from incident_copilot.controlplane.pg_store import PostgresControlPlaneStore
from incident_copilot.db import repositories as repo
from incident_copilot.db.models import (
    ApprovalToken,
    EventType,
    Incident,
    IncidentStatus,
    Job,
    Proposal,
    ProposalStatus,
)
from incident_copilot.jobs import queue
from incident_copilot.jobs.handlers import investigate as handler_module
from incident_copilot.retrieval.index import reindex
from tests.fake_anthropic import FakeAnthropic, make_proposal


@pytest.fixture(autouse=True)
def indexed(db: Session) -> Iterator[None]:
    reindex(db)
    PostgresControlPlaneStore().get_in(db, "payments_gateway_timeout")
    db.commit()
    yield


@pytest.fixture
def incident(db: Session) -> Incident:
    inc = repo.create_incident(
        db,
        scenario_key="payments_gateway_timeout",
        service="payments-api",
        severity="SEV2",  # the alert's guess; the agent has evidence it does not
        title="Payments failing",
        region="us-east",
    )
    db.commit()
    return inc


@pytest.fixture
def job(db: Session, incident: Incident) -> Job:
    j = queue.enqueue(db, "investigate", {}, incident_id=incident.id)
    db.commit()
    return j


def run(db: Session, job: Job, client: Any) -> None:
    """Invoke the handler with a stubbed client."""
    original = handler_module.run_agent

    def patched(session: Session, scenario: Any, state: Any, **kwargs: Any) -> Any:
        return original(session, scenario, state, client=client, **kwargs)

    handler_module.run_agent = patched  # type: ignore[assignment]
    try:
        handler_module.handle_investigate(db, job)
    finally:
        handler_module.run_agent = original  # type: ignore[assignment]


# -- the happy path ---------------------------------------------------------


def test_handler_records_a_pending_proposal(db: Session, incident: Incident, job: Job) -> None:
    run(db, job, FakeAnthropic(proposal=make_proposal()))
    db.commit()

    proposal = db.query(Proposal).one()
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.hypothesis
    assert proposal.actions[0]["kind"] == "set_config_value"
    assert incident.status is IncidentStatus.AWAITING_APPROVAL


def test_handler_writes_the_evidence_trail(db: Session, incident: Incident, job: Job) -> None:
    """The transcript is what lets a citation be audited rather than trusted."""
    run(db, job, FakeAnthropic(proposal=make_proposal()))
    db.commit()

    events = {e.type: e for e in repo.timeline(db, incident)}
    assert EventType.INVESTIGATION_STARTED in events
    evidence = events[EventType.EVIDENCE_GATHERED].payload
    assert evidence["tool_calls"] == 6
    assert "get_metrics()" in evidence["transcript"]
    assert evidence["cited_chunks"]
    assert evidence["input_tokens"] > 0


def test_handler_issues_both_approval_tokens(db: Session, job: Job) -> None:
    run(db, job, FakeAnthropic(proposal=make_proposal()))
    db.commit()

    tokens = db.query(ApprovalToken).all()
    assert {t.decision for t in tokens} == {"approve", "reject"}
    assert all(t.consumed_at is None for t in tokens)


def test_handler_reclassifies_severity_from_evidence(
    db: Session, incident: Incident, job: Job
) -> None:
    """The alert's severity is a guess; the agent has telemetry the alert did not."""
    assert incident.severity == "SEV2"
    run(db, job, FakeAnthropic(proposal=make_proposal(severity="SEV1")))
    db.commit()

    assert incident.severity == "SEV1"
    notes = [e for e in repo.timeline(db, incident) if e.type is EventType.NOTE_ADDED]
    assert notes and notes[-1].payload["note"] == "severity_reclassified"


def test_no_action_proposal_is_recorded_like_any_other(db: Session, job: Job) -> None:
    """'Nothing is wrong' is a decision worth auditing, not an empty result."""
    proposal = make_proposal(
        severity="SEV3",
        actions=[
            {
                "kind": "no_action",
                "reason": "All metrics inside SLO",
                "rationale": "Autoscaler already absorbed the surge",
                "risk": "low",
                "reversible": True,
            }
        ],
    )
    run(db, job, FakeAnthropic(proposal=proposal))
    db.commit()

    stored = db.query(Proposal).one()
    assert stored.actions[0]["kind"] == "no_action"
    assert stored.status is ProposalStatus.PENDING


# -- idempotency ------------------------------------------------------------


def test_redelivery_does_not_run_the_agent_twice(db: Session, incident: Incident, job: Job) -> None:
    """At-least-once delivery must not mean paying for two model calls."""
    client = FakeAnthropic(proposal=make_proposal())
    run(db, job, client)
    db.commit()
    run(db, job, client)
    db.commit()

    assert len(client.investigate_calls) == 1
    assert db.query(Proposal).count() == 1


def test_a_decided_proposal_does_not_block_re_investigation(
    db: Session, incident: Incident, job: Job
) -> None:
    """After a failed verification the agent must be able to propose again."""
    client = FakeAnthropic(proposal=make_proposal())
    run(db, job, client)
    db.commit()

    first = db.query(Proposal).one()
    repo.decide_proposal(db, first, approved=True, actor="U1")
    db.commit()

    run(db, job, FakeAnthropic(proposal=make_proposal(hypothesis="Second theory")))
    db.commit()
    assert db.query(Proposal).count() == 2


# -- failure paths ----------------------------------------------------------


def test_agent_failure_is_recorded_then_raised(db: Session, incident: Incident, job: Job) -> None:
    """The queue needs the exception to retry; the thread needs the event to explain itself."""
    with pytest.raises(AgentError):
        run(db, job, FakeAnthropic(script=[], proposal=make_proposal()))

    errors = [e for e in repo.timeline(db, incident) if e.type is EventType.ERROR]
    assert errors and errors[-1].payload["stage"] == "investigate"


def test_unexecutable_action_is_rejected_before_a_card_exists(
    db: Session, incident: Incident, job: Job
) -> None:
    """A button that cannot execute must never be rendered."""
    broken = make_proposal(
        actions=[
            {
                "kind": "rollback_deploy",  # missing `service`
                "rationale": "roll it back",
                "risk": "medium",
                "reversible": True,
            }
        ]
    )
    with pytest.raises(AgentError, match="not executable"):
        run(db, job, FakeAnthropic(proposal=broken))

    assert db.query(Proposal).count() == 0
    errors = [e for e in repo.timeline(db, incident) if e.type is EventType.ERROR]
    assert errors and errors[-1].payload["stage"] == "propose"


def test_job_without_an_incident_fails_loudly(db: Session) -> None:
    orphan = queue.enqueue(db, "investigate", {})
    db.commit()
    with pytest.raises(ValueError, match="no resolvable incident"):
        run(db, orphan, FakeAnthropic(proposal=make_proposal()))


# -- the join to the control plane ------------------------------------------


def test_the_stored_proposal_is_executable(db: Session, incident: Incident, job: Job) -> None:
    """The end of phase 4 must hand phase 6 something it can actually apply."""
    from incident_copilot.agent.schemas import ProposedAction
    from incident_copilot.controlplane.actions import apply_action
    from incident_copilot.controlplane.scenarios import get_registry
    from incident_copilot.controlplane.simulator import assess_health

    run(db, job, FakeAnthropic(proposal=make_proposal()))
    db.commit()

    stored = db.query(Proposal).one()
    scenario = get_registry().get(incident.scenario_key)
    state = PostgresControlPlaneStore().get_in(db, incident.scenario_key)
    assert not assess_health(scenario, state).healthy

    for payload in stored.actions:
        state = apply_action(state, ProposedAction.model_validate(payload).to_action()).state
    assert assess_health(scenario, state).healthy
