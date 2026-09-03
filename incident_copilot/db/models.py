"""SQLAlchemy declarative models.

Two conventions run through this schema:

* **The incident timeline is append-only.** `incident_events` is the record of what happened;
  `incidents.status` is a projection of it. Nothing rewrites history, so the audit trail, the
  postmortem source, and the "what happened with INC-1234" answer are all the same data.
* **Enums are stored as VARCHAR with a CHECK constraint**, not native Postgres enums. Adding a
  value to a native enum requires `ALTER TYPE` and cannot run inside some transactional
  migrations; a check constraint is a plain DDL swap.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """`created_at` / `updated_at` maintained by the database, not the application.

    Server-side defaults mean rows written by migrations, fixtures, or psql get correct
    timestamps too — an application-side default only covers rows the ORM happens to write.
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def _enum(python_enum: type[enum.Enum], name: str) -> Enum:
    """VARCHAR + CHECK rather than a native Postgres enum type."""
    return Enum(
        python_enum, name=name, native_enum=False, values_callable=lambda e: [m.value for m in e]
    )


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class IncidentStatus(enum.StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    REMEDIATING = "remediating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    NEEDS_ATTENTION = "needs_attention"
    CLOSED = "closed"


class EventType(enum.StrEnum):
    CREATED = "created"
    INVESTIGATION_STARTED = "investigation_started"
    EVIDENCE_GATHERED = "evidence_gathered"
    PROPOSAL_CREATED = "proposal_created"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTION_EXECUTED = "action_executed"
    ACTION_FAILED = "action_failed"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    RESOLVED = "resolved"
    NOTE_ADDED = "note_added"
    ERROR = "error"


class ProposalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ExecutionStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


class Incident(Base, TimestampMixin):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    scenario_key: Mapped[str] = mapped_column(String(128), index=True)
    service: Mapped[str] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(64), default=None)
    severity: Mapped[str] = mapped_column(String(8))
    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus, "incident_status"), default=IncidentStatus.OPEN, index=True
    )
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, default=None)

    # Slack coordinates. Nullable because an incident can be created by the API with no thread.
    slack_channel_id: Mapped[str | None] = mapped_column(String(32), default=None)
    slack_thread_ts: Mapped[str | None] = mapped_column(String(32), default=None)

    opened_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)

    events: Mapped[list[IncidentEvent]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="IncidentEvent.seq"
    )
    proposals: Mapped[list[Proposal]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="Proposal.created_at"
    )

    __table_args__ = (
        CheckConstraint("severity IN ('SEV1','SEV2','SEV3')", name="severity_valid"),
        Index("ix_incidents_slack_thread", "slack_channel_id", "slack_thread_ts"),
    )


class IncidentEvent(Base):
    """One entry in an incident's append-only timeline.

    `seq` is per-incident and monotonic, giving a stable ordering that timestamps alone cannot
    (two events written in the same millisecond would tie). The unique constraint on
    (incident_id, seq) is what makes concurrent appends detectable rather than silently
    interleaved — see `repositories.append_event`.
    """

    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[EventType] = mapped_column(_enum(EventType, "event_type"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    incident: Mapped[Incident] = relationship(back_populates="events")

    __table_args__ = (UniqueConstraint("incident_id", "seq", name="incident_id_seq"),)


# ---------------------------------------------------------------------------
# Proposals, approvals, executions
# ---------------------------------------------------------------------------


class Proposal(Base, TimestampMixin):
    """A structured remediation proposal produced by the agent."""

    __tablename__ = "proposals"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ProposalStatus] = mapped_column(
        _enum(ProposalStatus, "proposal_status"), default=ProposalStatus.PENDING, index=True
    )
    severity: Mapped[str] = mapped_column(String(8))
    hypothesis: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16))
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evidence_cited: Mapped[list[str]] = mapped_column(JSONB, default=list)
    similar_incidents: Mapped[list[str]] = mapped_column(JSONB, default=list)
    verification_plan: Mapped[str | None] = mapped_column(Text, default=None)
    next_update_minutes: Mapped[int] = mapped_column(Integer, default=15)
    # The verbatim structured output, kept so a prompt change can be evaluated against what the
    # model actually returned rather than against our projection of it.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    decided_by: Mapped[str | None] = mapped_column(String(128), default=None)

    incident: Mapped[Incident] = relationship(back_populates="proposals")
    executions: Mapped[list[ActionExecution]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
        order_by="ActionExecution.action_index",
    )

    __table_args__ = (
        CheckConstraint("confidence IN ('low','medium','high')", name="confidence_valid"),
        CheckConstraint("severity IN ('SEV1','SEV2','SEV3')", name="severity_valid"),
    )


class ApprovalToken(Base):
    """A single-use, server-side token backing one Slack approval button.

    Slack button payloads are client-controlled: whatever is put in `value` comes back from the
    browser and must never be trusted as an instruction. The button therefore carries only an
    opaque token, and the decision it authorises is looked up here.

    `consumed_at` makes the token single-use, so a replayed interaction — or a stale card
    someone scrolls back to and clicks a day later — cannot re-fire an action.
    """

    __tablename__ = "approval_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("proposals.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16))
    action_index: Mapped[int | None] = mapped_column(
        Integer, default=None, doc="None authorises every action in the proposal."
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    consumed_by: Mapped[str | None] = mapped_column(String(128), default=None)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (CheckConstraint("decision IN ('approve','reject')", name="decision_valid"),)


class ActionExecution(Base, TimestampMixin):
    """One approved action, and what happened when it ran.

    `state_before` / `state_after` are full control-plane snapshots. Storing both makes a failed
    verification attributable to a specific transition instead of to the incident as a whole.
    """

    __tablename__ = "action_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("proposals.id", ondelete="CASCADE"), index=True
    )
    action_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus, "execution_status"), default=ExecutionStatus.PENDING
    )
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    state_before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    state_after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)

    proposal: Mapped[Proposal] = relationship(back_populates="executions")

    __table_args__ = (
        UniqueConstraint("proposal_id", "action_index", name="proposal_id_action_index"),
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class Job(Base):
    """A unit of background work.

    Slack allows three seconds to acknowledge an interaction. Everything slower than that —
    which is all of the interesting work — becomes a row here, claimed by a worker process.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Set by the enqueuer to make retried Slack deliveries collapse onto one job.
    idem_key: Mapped[str | None] = mapped_column(String(200), unique=True, default=None)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    locked_by: Mapped[str | None] = mapped_column(String(128), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)

    __table_args__ = (
        # Partial index: the claim query only ever scans pending rows, and keeping finished jobs
        # out of the index stops it growing with history.
        Index(
            "ix_jobs_claimable",
            "run_after",
            postgresql_where=("status = 'pending'"),
        ),
        Index("ix_jobs_stalled", "locked_at", postgresql_where=("status = 'running'")),
    )


class SlackDelivery(Base):
    """Dedupe record for inbound Slack deliveries.

    Slack redelivers any interaction it does not get a 2xx for, and a redelivery is
    indistinguishable from a fresh click at the handler. Recording the delivery key under a
    unique constraint turns "have I seen this?" into an insert that either succeeds or conflicts.
    """

    __tablename__ = "slack_deliveries"

    delivery_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Control plane + knowledge base
# ---------------------------------------------------------------------------


class ControlPlaneStateRow(Base):
    """Persisted control-plane state, one row per scenario.

    `version` is an optimistic-concurrency counter: a writer that read version N may only write
    version N+1. Two workers racing to remediate the same incident cannot silently clobber each
    other's state — the loser is told to re-read.
    """

    __tablename__ = "control_plane_state"

    scenario_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KBChunk(Base):
    """A retrievable chunk of a runbook, policy, or resolved incident.

    Phase 3 adds a `vector` column alongside the full-text index; the lexical half works on its
    own, which is what the MVP track relies on.
    """

    __tablename__ = "kb_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    corpus: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (CheckConstraint("corpus IN ('kb','history')", name="corpus_valid"),)
