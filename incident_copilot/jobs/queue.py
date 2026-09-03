"""Postgres-backed job queue.

This replaces the `threading.Thread` the previous version spawned from its Slack handler. That
thread died with the process, had no retry, no visibility, and no backpressure — a job lost to a
deploy was simply lost, with the Slack thread left saying "running backend workflow now…".

Delivery is **at-least-once**. A worker can die between doing the work and marking the job done,
so the job runs again. Handlers must therefore be idempotent; the control plane's `ActionError`
on redundant actions is one of the mechanisms that makes them so.

Why Postgres and not Redis: the durability, the visibility (a job's history is queryable with
psql), and one fewer moving part in the demo. `FOR UPDATE SKIP LOCKED` has been the standard way
to do this since Postgres 9.5 and comfortably outruns what this workload needs.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Select, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from incident_copilot.db.models import Job, JobStatus

# Backoff grows 2^attempts * base, so attempt 1 waits ~30s, attempt 2 ~60s, attempt 3 ~120s.
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
# Up to ±25% jitter, so a batch of jobs failing on the same downstream outage does not retry in
# lockstep and re-create the spike that caused the failure.
BACKOFF_JITTER = 0.25


def _now() -> datetime:
    return datetime.now(UTC)


def backoff_delay(attempts: int, *, jitter: bool = True) -> timedelta:
    """Exponential backoff with jitter, capped."""
    raw = min(BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)), BACKOFF_MAX_SECONDS)
    if jitter:
        raw *= 1 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER)
    return timedelta(seconds=raw)


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    idem_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
    incident_id: UUID | None = None,
) -> Job:
    """Add a job to the queue.

    When `idem_key` is supplied and already present, the existing job is returned untouched
    rather than a duplicate being created. This is what makes a Slack redelivery — or a
    double-clicked button — land as one unit of work.
    """
    values: dict[str, Any] = {
        "kind": kind,
        "payload": payload or {},
        "idem_key": idem_key,
        "status": JobStatus.PENDING,
        "max_attempts": max_attempts,
        "run_after": run_after or _now(),
        "incident_id": incident_id,
    }

    if idem_key is None:
        job = Job(**values)
        session.add(job)
        session.flush()
        return job

    # ON CONFLICT DO NOTHING, then read back — one round trip in the common case, and correct
    # under concurrency in a way that SELECT-then-INSERT is not.
    stmt = pg_insert(Job).values(**values).on_conflict_do_nothing(index_elements=["idem_key"])
    session.execute(stmt)
    session.flush()
    existing = session.execute(select(Job).where(Job.idem_key == idem_key)).scalar_one()
    return existing


def _claimable(kinds: list[str] | None) -> Select[tuple[int]]:
    stmt = (
        select(Job.id)
        .where(Job.status == JobStatus.PENDING, Job.run_after <= _now())
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    return stmt


def claim(session: Session, worker_id: str, *, kinds: list[str] | None = None) -> Job | None:
    """Atomically claim the next runnable job, or return None.

    `FOR UPDATE SKIP LOCKED` on the inner select is what allows N workers to poll the same table
    concurrently: each row is locked by at most one transaction, and a worker that finds a row
    locked skips past it instead of blocking behind it.
    """
    job_id = session.execute(_claimable(kinds)).scalar_one_or_none()
    if job_id is None:
        return None

    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=JobStatus.RUNNING,
            attempts=Job.attempts + 1,
            locked_at=_now(),
            locked_by=worker_id,
        )
    )
    session.flush()
    job = session.get(Job, job_id)
    assert job is not None, "claimed job disappeared mid-transaction"
    # The UPDATE above bypassed the ORM, so a previously-identity-mapped instance would still
    # show the pre-claim status.
    session.refresh(job)
    return job


def complete(session: Session, job: Job) -> None:
    session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(
            status=JobStatus.SUCCEEDED,
            locked_at=None,
            locked_by=None,
            finished_at=_now(),
            last_error=None,
        )
    )
    session.flush()


def fail(session: Session, job: Job, error: str) -> JobStatus:
    """Record a failure, then either schedule a retry or bury the job.

    Returns the resulting status so the caller can decide whether to notify — a job with retries
    left is noise, a dead job needs a human.
    """
    exhausted = job.attempts >= job.max_attempts
    status = JobStatus.DEAD if exhausted else JobStatus.PENDING
    values: dict[str, Any] = {
        "status": status,
        "locked_at": None,
        "locked_by": None,
        # Truncated: a stack trace in a queue table is a liability, and the useful part is the
        # head of the message.
        "last_error": error[:2000],
    }
    if exhausted:
        values["finished_at"] = _now()
    else:
        values["run_after"] = _now() + backoff_delay(job.attempts)

    session.execute(update(Job).where(Job.id == job.id).values(**values))
    session.flush()
    return status


def reap_stalled(session: Session, visibility_timeout_seconds: int) -> int:
    """Return jobs whose worker died mid-flight to the pending pool.

    Without this, a `kill -9` between claim and complete strands the job as `running` forever.
    The claim already incremented `attempts`, so a job that reliably kills its worker still
    exhausts its retries and lands in `dead` rather than looping indefinitely.
    """
    cutoff = _now() - timedelta(seconds=visibility_timeout_seconds)
    result = session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
        .values(
            status=JobStatus.PENDING,
            locked_at=None,
            locked_by=None,
            run_after=_now(),
            last_error="reclaimed after worker stalled",
        )
    )
    session.flush()
    # execute() is typed as Result; only the cursor result carries a row count.
    return cast("CursorResult[Any]", result).rowcount


def queue_depth(session: Session) -> dict[str, int]:
    """Pending/running/dead counts, for health output and tests."""
    rows = session.execute(
        select(Job.status, func.count()).select_from(Job).group_by(Job.status)
    ).all()
    return {str(status): count for status, count in rows}
