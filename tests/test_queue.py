"""Job queue semantics, including the concurrency behaviour that motivates the design."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from incident_copilot.db.models import Job, JobStatus
from incident_copilot.jobs import queue


def _now() -> datetime:
    return datetime.now(UTC)


# -- basics -----------------------------------------------------------------


def test_enqueue_then_claim_round_trip(db: Session) -> None:
    queue.enqueue(db, "investigate", {"incident": "INC-1"})
    db.commit()

    job = queue.claim(db, "worker-1")
    assert job is not None
    assert job.kind == "investigate"
    assert job.payload == {"incident": "INC-1"}
    assert job.status is JobStatus.RUNNING
    assert job.locked_by == "worker-1"
    assert job.attempts == 1


def test_empty_queue_returns_none(db: Session) -> None:
    assert queue.claim(db, "worker-1") is None


def test_claim_respects_run_after(db: Session) -> None:
    queue.enqueue(db, "later", run_after=_now() + timedelta(minutes=5))
    db.commit()
    assert queue.claim(db, "worker-1") is None


def test_claim_is_fifo_by_run_after(db: Session) -> None:
    queue.enqueue(db, "second", run_after=_now() - timedelta(seconds=10))
    queue.enqueue(db, "first", run_after=_now() - timedelta(seconds=60))
    db.commit()

    assert (first := queue.claim(db, "w")) is not None and first.kind == "first"
    assert (second := queue.claim(db, "w")) is not None and second.kind == "second"


def test_claim_can_filter_by_kind(db: Session) -> None:
    queue.enqueue(db, "investigate")
    queue.enqueue(db, "verify")
    db.commit()

    job = queue.claim(db, "w", kinds=["verify"])
    assert job is not None and job.kind == "verify"


def test_complete_marks_the_job_done(db: Session) -> None:
    queue.enqueue(db, "k")
    db.commit()
    job = queue.claim(db, "w")
    assert job is not None

    queue.complete(db, job)
    db.commit()
    db.refresh(job)
    assert job.status is JobStatus.SUCCEEDED
    assert job.finished_at is not None
    assert job.locked_by is None


# -- idempotency ------------------------------------------------------------


def test_idem_key_collapses_duplicate_enqueues(db: Session) -> None:
    """A Slack redelivery must not create a second unit of work."""
    first = queue.enqueue(db, "investigate", {"a": 1}, idem_key="slack:evt-123")
    db.commit()
    second = queue.enqueue(db, "investigate", {"a": 2}, idem_key="slack:evt-123")
    db.commit()

    assert first.id == second.id
    assert second.payload == {"a": 1}  # the original wins; the duplicate is discarded
    assert db.query(Job).count() == 1


def test_enqueues_without_idem_key_are_distinct(db: Session) -> None:
    queue.enqueue(db, "k")
    queue.enqueue(db, "k")
    db.commit()
    assert db.query(Job).count() == 2


# -- retries and backoff ----------------------------------------------------


def test_failure_reschedules_with_backoff(db: Session) -> None:
    queue.enqueue(db, "k", max_attempts=3)
    db.commit()
    job = queue.claim(db, "w")
    assert job is not None

    status = queue.fail(db, job, "boom")
    db.commit()
    db.refresh(job)

    assert status is JobStatus.PENDING
    assert job.status is JobStatus.PENDING
    assert job.last_error == "boom"
    assert job.run_after > _now()  # not immediately runnable again
    assert job.locked_by is None


def test_retry_is_not_claimable_until_backoff_elapses(db: Session) -> None:
    queue.enqueue(db, "k")
    db.commit()
    job = queue.claim(db, "w")
    assert job is not None
    queue.fail(db, job, "boom")
    db.commit()

    assert queue.claim(db, "w") is None


def test_job_dies_after_max_attempts(db: Session) -> None:
    queue.enqueue(db, "k", max_attempts=2)
    db.commit()

    for _ in range(2):
        job = queue.claim(db, "w")
        assert job is not None
        status = queue.fail(db, job, "boom")
        db.commit()
        job.run_after = _now() - timedelta(seconds=1)  # skip the wait
        db.commit()

    assert status is JobStatus.DEAD
    db.refresh(job)
    assert job.status is JobStatus.DEAD
    assert job.finished_at is not None
    assert queue.claim(db, "w") is None


def test_long_errors_are_truncated(db: Session) -> None:
    """A full stack trace in a queue table is a liability, not evidence."""
    queue.enqueue(db, "k")
    db.commit()
    job = queue.claim(db, "w")
    assert job is not None
    queue.fail(db, job, "x" * 5000)
    db.commit()
    db.refresh(job)
    assert job.last_error is not None and len(job.last_error) == 2000


def test_backoff_grows_and_is_capped() -> None:
    without_jitter = [queue.backoff_delay(n, jitter=False).total_seconds() for n in range(1, 12)]
    assert without_jitter[:3] == [30, 60, 120]
    assert without_jitter == sorted(without_jitter)
    assert max(without_jitter) == queue.BACKOFF_MAX_SECONDS


def test_backoff_jitter_spreads_retries() -> None:
    """Without jitter a batch failing together retries together and recreates the spike."""
    samples = {queue.backoff_delay(3).total_seconds() for _ in range(50)}
    assert len(samples) > 1
    base = queue.backoff_delay(3, jitter=False).total_seconds()
    assert all(base * 0.7 <= s <= base * 1.3 for s in samples)


# -- reaper -----------------------------------------------------------------


def test_reaper_reclaims_a_stalled_job(db: Session) -> None:
    """The `kill -9` case: a worker dies holding a claim."""
    queue.enqueue(db, "k")
    db.commit()
    job = queue.claim(db, "doomed-worker")
    assert job is not None

    job.locked_at = _now() - timedelta(seconds=600)
    db.commit()

    assert queue.reap_stalled(db, visibility_timeout_seconds=300) == 1
    db.commit()
    db.refresh(job)
    assert job.status is JobStatus.PENDING
    assert job.locked_by is None

    reclaimed = queue.claim(db, "healthy-worker")
    assert reclaimed is not None and reclaimed.id == job.id


def test_reaper_leaves_healthy_jobs_alone(db: Session) -> None:
    queue.enqueue(db, "k")
    db.commit()
    queue.claim(db, "w")
    db.commit()
    assert queue.reap_stalled(db, visibility_timeout_seconds=300) == 0


def test_reclaimed_job_still_exhausts_its_retries(db: Session) -> None:
    """A job that reliably kills its worker must eventually die, not loop forever."""
    queue.enqueue(db, "poison", max_attempts=2)
    db.commit()

    for _ in range(2):
        job = queue.claim(db, "w")
        assert job is not None
        job.locked_at = _now() - timedelta(seconds=600)
        db.commit()
        queue.reap_stalled(db, visibility_timeout_seconds=300)
        db.commit()

    db.refresh(job)
    assert job.attempts == 2
    # Attempts are spent, so the next failure buries it rather than retrying again.
    job = queue.claim(db, "w")
    assert job is not None
    assert queue.fail(db, job, "poison") is JobStatus.DEAD


# -- concurrency ------------------------------------------------------------


def _claim_in_thread(
    factory: sessionmaker[Session], worker_id: str, sink: list[int | None]
) -> Callable[[], None]:
    def run() -> None:
        with factory() as session:
            job = queue.claim(session, worker_id)
            sink.append(job.id if job else None)
            session.commit()

    return run


def test_concurrent_workers_never_claim_the_same_job(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """The property SKIP LOCKED exists to provide.

    Ten jobs, ten workers racing. Every job must go to exactly one worker — no duplicates, none
    dropped, and no worker blocked behind another's row lock.
    """
    for i in range(10):
        queue.enqueue(db, "work", {"i": i})
    db.commit()

    claimed: list[int | None] = []
    barrier = threading.Barrier(10)

    def racer(worker_id: str) -> None:
        with session_factory() as session:
            barrier.wait(timeout=10)  # maximise the overlap
            job = queue.claim(session, worker_id)
            claimed.append(job.id if job else None)
            session.commit()

    threads = [threading.Thread(target=racer, args=(f"w-{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    ids = [job_id for job_id in claimed if job_id is not None]
    assert len(ids) == 10, "some workers came away empty — rows were being skipped"
    assert len(set(ids)) == 10, "the same job was claimed twice"


def test_more_workers_than_jobs_leaves_the_extras_empty_handed(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """Contention must not produce phantom claims or deadlock."""
    queue.enqueue(db, "only-one")
    db.commit()

    results: list[int | None] = []
    threads = [
        threading.Thread(target=_claim_in_thread(session_factory, f"w-{i}", results))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len([r for r in results if r is not None]) == 1
    assert len(results) == 8


def test_concurrent_enqueue_with_the_same_idem_key_creates_one_job(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """Two simultaneous redeliveries of the same Slack interaction."""
    errors: list[Exception] = []
    barrier = threading.Barrier(5)

    def racer() -> None:
        try:
            with session_factory() as session:
                barrier.wait(timeout=10)
                queue.enqueue(session, "investigate", {"x": 1}, idem_key="slack:same")
                session.commit()
        except Exception as exc:  # noqa: BLE001 — recorded and asserted on below
            errors.append(exc)

    threads = [threading.Thread(target=racer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert db.query(Job).filter(Job.idem_key == "slack:same").count() == 1
    # Serialisation failures are acceptable (the caller retries); duplicates are not.
    assert all("duplicate key" not in str(e).lower() for e in errors)


# -- introspection ----------------------------------------------------------


def test_queue_depth_reports_counts(db: Session) -> None:
    queue.enqueue(db, "a")
    queue.enqueue(db, "b")
    db.commit()
    queue.claim(db, "w")
    db.commit()

    depth = queue.queue_depth(db)
    assert depth.get(JobStatus.PENDING) == 1
    assert depth.get(JobStatus.RUNNING) == 1


@pytest.mark.parametrize("attempts", [0, 1, 5])
def test_backoff_never_negative(attempts: int) -> None:
    assert queue.backoff_delay(attempts).total_seconds() > 0
