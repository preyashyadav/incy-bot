"""Incident timeline, proposals, approval tokens, and executions."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from incident_copilot.db import repositories as repo
from incident_copilot.db.models import (
    EventType,
    ExecutionStatus,
    Incident,
    IncidentEvent,
    IncidentStatus,
    ProposalStatus,
)


def make_incident(session: Session, **overrides: object) -> Incident:
    kwargs: dict[str, object] = {
        "scenario_key": "payments_gateway_timeout",
        "service": "payments-api",
        "severity": "SEV1",
        "title": "Payments failing",
        "region": "us-east",
    }
    kwargs.update(overrides)
    return repo.create_incident(session, **kwargs)  # type: ignore[arg-type]


# -- creation and timeline --------------------------------------------------


def test_new_incident_starts_open_with_a_created_event(db: Session) -> None:
    incident = make_incident(db)
    db.commit()

    assert incident.key.startswith("INC-")
    assert incident.status is IncidentStatus.OPEN
    events = repo.timeline(db, incident)
    assert [e.type for e in events] == [EventType.CREATED]
    assert events[0].seq == 1


def test_incident_keys_are_unique(db: Session) -> None:
    keys = {make_incident(db).key for _ in range(20)}
    db.commit()
    assert len(keys) == 20


def test_events_are_sequenced_monotonically(db: Session) -> None:
    incident = make_incident(db)
    for event_type in (
        EventType.INVESTIGATION_STARTED,
        EventType.EVIDENCE_GATHERED,
        EventType.NOTE_ADDED,
    ):
        repo.append_event(db, incident, event_type)
    db.commit()

    assert [e.seq for e in repo.timeline(db, incident)] == [1, 2, 3, 4]


def test_sequences_are_per_incident(db: Session) -> None:
    first, second = make_incident(db), make_incident(db)
    repo.append_event(db, first, EventType.NOTE_ADDED)
    db.commit()

    assert [e.seq for e in repo.timeline(db, first)] == [1, 2]
    assert [e.seq for e in repo.timeline(db, second)] == [1]


def test_status_is_a_projection_of_the_timeline(db: Session) -> None:
    """Status is derived, never set directly — that is what keeps the audit trail honest."""
    incident = make_incident(db)
    for event_type, expected in [
        (EventType.INVESTIGATION_STARTED, IncidentStatus.INVESTIGATING),
        (EventType.PROPOSAL_CREATED, IncidentStatus.AWAITING_APPROVAL),
        (EventType.APPROVED, IncidentStatus.REMEDIATING),
        (EventType.VERIFIED, IncidentStatus.RESOLVED),
    ]:
        repo.append_event(db, incident, event_type)
        assert incident.status is expected
    db.commit()


def test_non_status_events_do_not_move_the_incident(db: Session) -> None:
    incident = make_incident(db)
    repo.append_event(db, incident, EventType.INVESTIGATION_STARTED)
    repo.append_event(db, incident, EventType.EVIDENCE_GATHERED)
    repo.append_event(db, incident, EventType.NOTE_ADDED)
    db.commit()
    assert incident.status is IncidentStatus.INVESTIGATING


def test_resolution_stamps_resolved_at_once(db: Session) -> None:
    incident = make_incident(db)
    assert incident.resolved_at is None
    repo.append_event(db, incident, EventType.VERIFIED)
    first = incident.resolved_at
    assert first is not None

    repo.append_event(db, incident, EventType.RESOLVED)
    db.commit()
    assert incident.resolved_at == first  # not overwritten by a later resolution event


def test_failed_verification_needs_attention(db: Session) -> None:
    incident = make_incident(db)
    repo.append_event(db, incident, EventType.APPROVED)
    repo.append_event(db, incident, EventType.VERIFICATION_FAILED, {"reason": "error_rate 0.124"})
    db.commit()
    assert incident.status is IncidentStatus.NEEDS_ATTENTION
    assert incident.resolved_at is None


def test_concurrent_appends_do_not_collide(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """MAX(seq)+1 races; the unique constraint catches it and the repository retries."""
    incident = make_incident(db)
    db.commit()
    incident_id = incident.id

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def racer() -> None:
        try:
            with session_factory() as session:
                loaded = session.get(Incident, incident_id)
                assert loaded is not None
                barrier.wait(timeout=10)
                repo.append_event(session, loaded, EventType.NOTE_ADDED)
                session.commit()
        except Exception as exc:  # noqa: BLE001 — asserted on below
            errors.append(exc)

    threads = [threading.Thread(target=racer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    seqs = [
        e.seq
        for e in db.query(IncidentEvent).filter(IncidentEvent.incident_id == incident_id).all()
    ]
    assert len(seqs) == len(set(seqs)), f"duplicate sequences {seqs}; errors={errors}"


# -- lookups ----------------------------------------------------------------


def test_lookup_by_key(db: Session) -> None:
    incident = make_incident(db)
    db.commit()
    assert repo.get_incident_by_key(db, incident.key) is not None
    assert repo.get_incident_by_key(db, "INC-NOPE") is None


def test_lookup_by_slack_thread(db: Session) -> None:
    """How a threaded reply finds the incident it belongs to."""
    make_incident(db, slack_channel_id="C123", slack_thread_ts="1700000000.000100")
    db.commit()

    assert repo.get_incident_by_thread(db, "C123", "1700000000.000100") is not None
    assert repo.get_incident_by_thread(db, "C123", "9999999999.000000") is None


# -- proposals --------------------------------------------------------------


def make_proposal(session: Session, incident: Incident, **overrides: object):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {
        "severity": "SEV1",
        "hypothesis": "Gateway timeout too low with the new client enabled",
        "confidence": "high",
        "actions": [{"kind": "set_config_value", "key": "gateway_timeout_ms", "value": 2000}],
        "evidence_cited": ["metrics:error_rate", "kb:rb-payments-001"],
    }
    kwargs.update(overrides)
    return repo.create_proposal(session, incident, **kwargs)  # type: ignore[arg-type]


def test_proposal_moves_the_incident_to_awaiting_approval(db: Session) -> None:
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    db.commit()

    assert proposal.status is ProposalStatus.PENDING
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert repo.timeline(db, incident)[-1].type is EventType.PROPOSAL_CREATED


def test_a_new_proposal_supersedes_the_pending_one(db: Session) -> None:
    """Otherwise the old card's buttons stay live and can apply a superseded remediation."""
    incident = make_incident(db)
    first = make_proposal(db, incident)
    second = make_proposal(db, incident, hypothesis="Actually the deploy")
    db.commit()

    assert first.status is ProposalStatus.SUPERSEDED
    assert second.status is ProposalStatus.PENDING


def test_decide_records_who_and_when(db: Session) -> None:
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    repo.decide_proposal(db, proposal, approved=True, actor="U123")
    db.commit()

    assert proposal.status is ProposalStatus.APPROVED
    assert proposal.decided_by == "U123"
    assert proposal.decided_at is not None
    assert incident.status is IncidentStatus.REMEDIATING

    last = repo.timeline(db, incident)[-1]
    assert last.type is EventType.APPROVED
    assert last.actor == "U123"


def test_rejection_flags_the_incident_for_a_human(db: Session) -> None:
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    repo.decide_proposal(db, proposal, approved=False, actor="U123")
    db.commit()
    assert proposal.status is ProposalStatus.REJECTED
    assert incident.status is IncidentStatus.NEEDS_ATTENTION


def test_expire_stale_proposals(db: Session) -> None:
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    token = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=1800)
    token.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    assert repo.expire_stale_proposals(db) == 1
    db.commit()
    assert proposal.status is ProposalStatus.EXPIRED


def test_live_proposals_are_not_expired(db: Session) -> None:
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=1800)
    db.commit()

    assert repo.expire_stale_proposals(db) == 0
    assert proposal.status is ProposalStatus.PENDING


# -- approval tokens --------------------------------------------------------


def test_tokens_are_opaque_and_unguessable(db: Session) -> None:
    """The Slack button carries this value, so it must reveal nothing and be unforgeable."""
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    tokens = {
        repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=60).token
        for _ in range(25)
    }
    db.commit()

    assert len(tokens) == 25
    assert all(len(t) >= 32 for t in tokens)
    assert all(str(proposal.id) not in t for t in tokens)


def test_token_can_be_consumed_once(db: Session) -> None:
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    token = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=1800)
    db.commit()

    consumed = repo.consume_approval_token(db, token.token, actor="U1")
    db.commit()
    assert consumed.consumed_by == "U1"

    with pytest.raises(repo.TokenRejected, match="already been used"):
        repo.consume_approval_token(db, token.token, actor="U2")


def test_expired_token_is_refused(db: Session) -> None:
    """A stale card someone scrolls back to tomorrow must not re-fire an action."""
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    token = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=-1)
    db.commit()

    with pytest.raises(repo.TokenRejected, match="expired"):
        repo.consume_approval_token(db, token.token, actor="U1")


def test_unknown_token_is_refused(db: Session) -> None:
    with pytest.raises(repo.TokenRejected, match="no longer valid"):
        repo.consume_approval_token(db, "forged-token", actor="attacker")


def test_only_one_of_two_simultaneous_clicks_wins(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """A double-click, or Slack redelivering the interaction, must not act twice."""
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    token = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=1800)
    db.commit()
    token_value = token.token

    accepted: list[str] = []
    rejected: list[str] = []
    barrier = threading.Barrier(4)

    def racer(actor: str) -> None:
        with session_factory() as session:
            barrier.wait(timeout=10)
            try:
                repo.consume_approval_token(session, token_value, actor=actor)
                session.commit()
                accepted.append(actor)
            except repo.TokenRejected:
                rejected.append(actor)

    threads = [threading.Thread(target=racer, args=(f"U{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(accepted) == 1, f"token consumed {len(accepted)} times"
    assert len(rejected) == 3


# -- executions -------------------------------------------------------------


def test_successful_execution_records_snapshots(db: Session) -> None:
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    execution = repo.record_execution(
        db, incident, proposal, action_index=0, kind="set_config_value", params={"value": 2000}
    )
    repo.finish_execution(
        db,
        execution,
        status=ExecutionStatus.SUCCEEDED,
        summary="Set gateway_timeout_ms 1000 → 2000",
        state_before={"v": 1},
        state_after={"v": 2},
    )
    db.commit()

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.state_before != execution.state_after
    assert execution.finished_at is not None
    assert repo.timeline(db, incident)[-1].type is EventType.ACTION_EXECUTED


def test_failed_execution_flags_the_incident(db: Session) -> None:
    incident = make_incident(db)
    proposal = make_proposal(db, incident)
    execution = repo.record_execution(
        db, incident, proposal, action_index=0, kind="rollback_deploy", params={}
    )
    repo.finish_execution(db, execution, status=ExecutionStatus.FAILED, error="no previous version")
    db.commit()

    assert repo.timeline(db, incident)[-1].type is EventType.ACTION_FAILED
    assert incident.status is IncidentStatus.NEEDS_ATTENTION


# -- slack delivery de-duplication ------------------------------------------


def test_first_delivery_is_not_a_duplicate(db: Session) -> None:
    assert repo.is_duplicate_delivery(db, "Ev123:0", "block_actions") is False
    db.commit()


def test_redelivery_is_detected(db: Session) -> None:
    repo.is_duplicate_delivery(db, "Ev123:0", "block_actions")
    db.commit()
    assert repo.is_duplicate_delivery(db, "Ev123:0", "block_actions") is True


def test_distinct_deliveries_are_independent(db: Session) -> None:
    assert repo.is_duplicate_delivery(db, "Ev123:0", "block_actions") is False
    assert repo.is_duplicate_delivery(db, "Ev123:1", "block_actions") is False
    db.commit()


def test_concurrent_redeliveries_admit_exactly_one(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    firsts: list[bool] = []
    barrier = threading.Barrier(5)

    def racer() -> None:
        with session_factory() as session:
            barrier.wait(timeout=10)
            try:
                firsts.append(repo.is_duplicate_delivery(session, "Ev-race", "block_actions"))
                session.commit()
            except Exception:
                firsts.append(True)  # serialisation failure == someone else got there first

    threads = [threading.Thread(target=racer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert firsts.count(False) == 1, "more than one caller was told it was the first delivery"
