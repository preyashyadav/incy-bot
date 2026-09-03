"""The approval flow, tested without Slack.

`service.py` is Slack-free by design, so the whole decision path — who may act, what a token
authorises, what happens on replay — is exercised here with plain function calls. That is the
part worth testing; the handlers above it only parse and reply.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from incident_copilot.controlplane.actions import SetConfigValue, apply_action
from incident_copilot.controlplane.pg_store import PostgresControlPlaneStore
from incident_copilot.controlplane.scenarios import get_registry
from incident_copilot.controlplane.simulator import assess_health
from incident_copilot.db import repositories as repo
from incident_copilot.db.models import (
    EventType,
    IncidentStatus,
    Job,
    JobStatus,
    ProposalStatus,
)
from incident_copilot.slack import service
from incident_copilot.slack.service import SlackActionRefused

SCENARIO = "payments_gateway_timeout"
CHANNEL = "C0INCIDENT"
USER = "U0PREYASH"


@pytest.fixture
def triaged(db: Session) -> Iterator[service.TriagedIncident]:
    result = service.triage_scenario(db, SCENARIO, channel_id=CHANNEL, actor=USER)
    db.commit()
    yield result


def make_real_proposal(db: Session, incident: object) -> tuple[object, object, object]:
    """A proposal with actions, plus its approve/reject tokens — what the agent produces."""
    proposal = repo.create_proposal(
        db,
        incident,  # type: ignore[arg-type]
        severity="SEV1",
        hypothesis="gateway_timeout_ms too low with the new client enabled",
        confidence="high",
        actions=[{"kind": "set_config_value", "key": "gateway_timeout_ms", "value": 2000}],
        verification_plan="error_rate returns below 1%",
    )
    approve = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=1800)
    reject = repo.issue_approval_token(db, proposal, decision="reject", ttl_seconds=1800)
    db.commit()
    return proposal, approve, reject


# -- triage -----------------------------------------------------------------


def test_triage_opens_an_incident_and_resets_the_world(db: Session) -> None:
    """The reset is what makes the demo repeatable across runs."""
    store = PostgresControlPlaneStore()
    state = store.get_in(db, SCENARIO)
    store.put_in(
        db, apply_action(state, SetConfigValue(key="gateway_timeout_ms", value=2000)).state
    )
    db.commit()
    assert assess_health(get_registry().get(SCENARIO), store.get_in(db, SCENARIO)).healthy

    triaged = service.triage_scenario(db, SCENARIO, channel_id=CHANNEL, actor=USER)
    db.commit()

    assert triaged.incident.key.startswith("INC-")
    assert triaged.incident.service == "payments-api"
    assert triaged.incident.status is IncidentStatus.AWAITING_APPROVAL
    # The scenario is broken again, so the incident is real.
    assert not assess_health(triaged.scenario, store.get_in(db, SCENARIO)).healthy


def test_triage_rejects_an_unknown_scenario(db: Session) -> None:
    with pytest.raises(SlackActionRefused, match="Available:"):
        service.triage_scenario(db, "not_a_scenario", channel_id=CHANNEL, actor=USER)


def test_triage_issues_a_gated_investigate_token(
    db: Session, triaged: service.TriagedIncident
) -> None:
    """Even 'start investigating' is token-gated, so a stale card cannot restart work."""
    assert triaged.investigate_token.decision == "approve"
    assert triaged.investigate_token.consumed_at is None
    assert len(triaged.investigate_token.token) >= 32


def test_attach_thread_records_where_the_incident_lives(
    db: Session, triaged: service.TriagedIncident
) -> None:
    service.attach_thread(db, triaged.incident, channel_id=CHANNEL, ts="1700000000.000100")
    db.commit()
    found = service.incident_for_thread(db, CHANNEL, "1700000000.000100")
    assert found is not None and found.key == triaged.incident.key


# -- starting an investigation ----------------------------------------------


def test_investigate_queues_exactly_one_job(db: Session, triaged: service.TriagedIncident) -> None:
    incident = service.start_investigation(db, triaged.investigate_token.token, actor=USER)
    db.commit()

    job = db.query(Job).one()
    assert job.kind == "investigate"
    assert job.incident_id == incident.id
    assert job.status is JobStatus.PENDING
    assert job.idem_key == f"investigate:{incident.id}"


def test_investigate_retires_the_placeholder_proposal(
    db: Session, triaged: service.TriagedIncident
) -> None:
    """The placeholder exists only to hang a token off; it must not look actionable."""
    service.start_investigation(db, triaged.investigate_token.token, actor=USER)
    db.commit()
    placeholder = triaged.incident.proposals[0]
    assert placeholder.status is ProposalStatus.SUPERSEDED


def test_investigate_token_is_single_use(db: Session, triaged: service.TriagedIncident) -> None:
    service.start_investigation(db, triaged.investigate_token.token, actor=USER)
    db.commit()
    with pytest.raises(SlackActionRefused, match="already been used"):
        service.start_investigation(db, triaged.investigate_token.token, actor="U0OTHER")


def test_forged_token_is_refused(db: Session) -> None:
    """The whole point of the design: a client-supplied value authorises nothing."""
    with pytest.raises(SlackActionRefused, match="no longer valid"):
        service.start_investigation(db, "definitely-not-a-real-token", actor="U0ATTACKER")


def test_missing_token_is_refused(db: Session) -> None:
    with pytest.raises(SlackActionRefused, match="missing its authorisation"):
        service.start_investigation(db, "", actor=USER)


def test_ignore_alert_records_the_decision(db: Session, triaged: service.TriagedIncident) -> None:
    incident = service.ignore_alert(db, triaged.investigate_token.token, actor=USER)
    db.commit()

    assert db.query(Job).count() == 0
    notes = [e for e in repo.timeline(db, incident) if e.type is EventType.NOTE_ADDED]
    assert notes and notes[-1].payload["note"] == "alert_ignored"
    assert notes[-1].actor == USER


# -- approving and rejecting ------------------------------------------------


def test_approval_records_the_decision_and_queues_execution(
    db: Session, triaged: service.TriagedIncident
) -> None:
    proposal, approve, _ = make_real_proposal(db, triaged.incident)

    decision = service.decide(db, approve.token, actor=USER)  # type: ignore[attr-defined]
    db.commit()

    assert decision.approved is True
    assert proposal.status is ProposalStatus.APPROVED  # type: ignore[attr-defined]
    assert proposal.decided_by == USER  # type: ignore[attr-defined]
    assert triaged.incident.status is IncidentStatus.REMEDIATING

    job = db.query(Job).filter(Job.kind == "execute_remediation").one()
    assert job.payload["proposal_id"] == str(proposal.id)  # type: ignore[attr-defined]
    assert job.idem_key == f"execute:{proposal.id}"  # type: ignore[attr-defined]


def test_rejection_makes_no_changes(db: Session, triaged: service.TriagedIncident) -> None:
    proposal, _, reject = make_real_proposal(db, triaged.incident)

    decision = service.decide(db, reject.token, actor=USER)  # type: ignore[attr-defined]
    db.commit()

    assert decision.approved is False
    assert proposal.status is ProposalStatus.REJECTED  # type: ignore[attr-defined]
    assert triaged.incident.status is IncidentStatus.NEEDS_ATTENTION
    assert db.query(Job).filter(Job.kind == "execute_remediation").count() == 0


def test_the_token_carries_the_decision_not_the_button(
    db: Session, triaged: service.TriagedIncident
) -> None:
    """A client that rewrites `action_id` still cannot turn a rejection into an approval.

    The handler never reads which button was pressed — it redeems a token, and the token says
    what it authorises.
    """
    proposal, _, reject = make_real_proposal(db, triaged.incident)
    decision = service.decide(db, reject.token, actor="U0ATTACKER")  # type: ignore[attr-defined]
    db.commit()
    assert decision.approved is False


def test_deciding_twice_is_refused(db: Session, triaged: service.TriagedIncident) -> None:
    _, approve, reject = make_real_proposal(db, triaged.incident)
    service.decide(db, approve.token, actor=USER)  # type: ignore[attr-defined]
    db.commit()

    with pytest.raises(SlackActionRefused, match="already approved"):
        service.decide(db, reject.token, actor="U0OTHER")  # type: ignore[attr-defined]


def test_expired_token_is_refused(db: Session, triaged: service.TriagedIncident) -> None:
    """A stale card scrolled back to tomorrow must not fire an action."""
    proposal = repo.create_proposal(
        db,
        triaged.incident,
        severity="SEV1",
        hypothesis="h",
        confidence="high",
        actions=[{"kind": "restart_service", "service": "payments-api"}],
    )
    expired = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=-1)
    db.commit()

    with pytest.raises(SlackActionRefused, match="expired"):
        service.decide(db, expired.token, actor=USER)


def test_two_simultaneous_approvals_admit_one(
    db: Session, triaged: service.TriagedIncident, session_factory: sessionmaker[Session]
) -> None:
    """A double-click, or Slack redelivering the interaction, must not remediate twice."""
    _, approve, _ = make_real_proposal(db, triaged.incident)
    token_value = approve.token  # type: ignore[attr-defined]

    accepted: list[str] = []
    refused: list[str] = []
    barrier = threading.Barrier(4)

    def racer(actor: str) -> None:
        with session_factory() as session:
            barrier.wait(timeout=10)
            try:
                service.decide(session, token_value, actor=actor)
                session.commit()
                accepted.append(actor)
            except Exception:
                session.rollback()
                refused.append(actor)

    threads = [threading.Thread(target=racer, args=(f"U{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(accepted) == 1, f"approved {len(accepted)} times"
    assert db.query(Job).filter(Job.kind == "execute_remediation").count() == 1


# -- lookups ----------------------------------------------------------------


def test_find_incident_is_case_insensitive(db: Session, triaged: service.TriagedIncident) -> None:
    assert service.find_incident(db, triaged.incident.key.lower()).id == triaged.incident.id


def test_find_incident_refuses_an_unknown_key(db: Session) -> None:
    with pytest.raises(SlackActionRefused, match="No incident found"):
        service.find_incident(db, "INC-NOPE")


def test_scenario_catalogue_lists_every_pack() -> None:
    catalogue = service.scenario_catalogue()
    assert len(catalogue) >= 5
    assert any(key == SCENARIO for key, _, _ in catalogue)


def test_latest_proposal_skips_the_placeholder(
    db: Session, triaged: service.TriagedIncident
) -> None:
    assert service.latest_proposal(db, triaged.incident) is None
    proposal, _, _ = make_real_proposal(db, triaged.incident)
    db.refresh(triaged.incident)
    assert service.latest_proposal(db, triaged.incident) is not None
