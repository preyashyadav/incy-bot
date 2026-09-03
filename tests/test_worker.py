"""Worker loop: dispatch, failure handling, shutdown."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from incident_copilot.config import Settings
from incident_copilot.db.models import Job, JobStatus
from incident_copilot.jobs import handlers, queue
from incident_copilot.jobs.worker import Worker, make_worker_id


@pytest.fixture(autouse=True)
def isolate_registry() -> Iterator[None]:
    """The handler registry is process-global; keep tests from leaking into each other."""
    yield
    for kind in list(handlers.registered_kinds()):
        handlers.unregister(kind)


@pytest.fixture
def worker(engine: Engine) -> Worker:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        worker_poll_interval_seconds=0.01,
        worker_visibility_timeout_seconds=300,
    )
    return Worker(settings, worker_id="test-worker")


# -- identity ---------------------------------------------------------------


def test_worker_ids_are_unique_and_traceable() -> None:
    """A stalled lock should name the host and pid that holds it."""
    first, second = make_worker_id(), make_worker_id()
    assert first != second
    assert first.count(":") == 2


# -- dispatch ---------------------------------------------------------------


def test_run_once_returns_false_on_an_empty_queue(db: Session, worker: Worker) -> None:
    assert worker.run_once() is False


def test_handler_runs_and_job_succeeds(db: Session, worker: Worker) -> None:
    seen: list[dict[str, object]] = []

    @handlers.register("greet")
    def _greet(session: Session, job: Job) -> None:
        seen.append(job.payload)

    queue.enqueue(db, "greet", {"name": "ada"})
    db.commit()

    assert worker.run_once() is True
    assert seen == [{"name": "ada"}]

    job = db.query(Job).one()
    db.refresh(job)
    assert job.status is JobStatus.SUCCEEDED
    assert job.finished_at is not None


def test_handler_writes_are_committed(db: Session, worker: Worker) -> None:
    """The handler shares the transaction that marks the job complete — both or neither."""

    @handlers.register("spawn")
    def _spawn(session: Session, job: Job) -> None:
        queue.enqueue(session, "child", {"parent": job.id})

    queue.enqueue(db, "spawn")
    db.commit()
    worker.run_once()

    kinds = {j.kind for j in db.query(Job).all()}
    assert kinds == {"spawn", "child"}


def test_handler_failure_rolls_back_its_writes(db: Session, worker: Worker) -> None:
    """A job that half-completed and then threw must not leave its partial writes behind."""

    @handlers.register("half")
    def _half(session: Session, job: Job) -> None:
        queue.enqueue(session, "orphan")
        raise RuntimeError("boom")

    queue.enqueue(db, "half")
    db.commit()
    worker.run_once()

    assert db.query(Job).filter(Job.kind == "orphan").count() == 0


def test_failure_schedules_a_retry(db: Session, worker: Worker) -> None:
    @handlers.register("flaky")
    def _flaky(session: Session, job: Job) -> None:
        raise ValueError("nope")

    queue.enqueue(db, "flaky", max_attempts=3)
    db.commit()
    worker.run_once()

    job = db.query(Job).one()
    db.refresh(job)
    assert job.status is JobStatus.PENDING
    assert job.attempts == 1
    assert job.last_error is not None and "ValueError: nope" in job.last_error


def test_unknown_kind_is_buried_immediately(db: Session, worker: Worker) -> None:
    """A missing handler is a deploy problem — retrying it three times only delays the alert."""
    queue.enqueue(db, "no-such-handler", max_attempts=5)
    db.commit()
    worker.run_once()

    job = db.query(Job).one()
    db.refresh(job)
    assert job.status is JobStatus.DEAD
    assert job.last_error is not None and "no handler registered" in job.last_error


def test_worker_only_takes_its_own_kinds(db: Session, engine: Engine) -> None:
    ran: list[str] = []

    @handlers.register("mine")
    def _mine(session: Session, job: Job) -> None:
        ran.append("mine")

    @handlers.register("theirs")
    def _theirs(session: Session, job: Job) -> None:
        ran.append("theirs")

    queue.enqueue(db, "theirs")
    db.commit()

    settings = Settings(_env_file=None, worker_poll_interval_seconds=0.01)  # type: ignore[call-arg]
    picky = Worker(settings, worker_id="picky", kinds=["mine"])
    assert picky.run_once() is False
    assert ran == []


def test_processed_counter_tracks_both_outcomes(db: Session, worker: Worker) -> None:
    @handlers.register("ok")
    def _ok(session: Session, job: Job) -> None:
        return None

    @handlers.register("bad")
    def _bad(session: Session, job: Job) -> None:
        raise RuntimeError("x")

    queue.enqueue(db, "ok")
    queue.enqueue(db, "bad")
    db.commit()

    worker.run_once()
    worker.run_once()
    assert worker.processed == 2


# -- registry ---------------------------------------------------------------


def test_duplicate_registration_is_rejected() -> None:
    """Silently replacing a handler would make behaviour depend on import order."""

    @handlers.register("dup")
    def _first(session: Session, job: Job) -> None: ...

    with pytest.raises(RuntimeError, match="already registered"):

        @handlers.register("dup")
        def _second(session: Session, job: Job) -> None: ...


def test_unknown_handler_error_lists_what_is_registered() -> None:
    @handlers.register("known")
    def _known(session: Session, job: Job) -> None: ...

    with pytest.raises(handlers.UnknownJobKind, match="known"):
        handlers.get_handler("missing")


# -- loop and shutdown ------------------------------------------------------


def test_run_forever_drains_then_stops(db: Session, worker: Worker) -> None:
    done: list[int] = []

    @handlers.register("batch")
    def _batch(session: Session, job: Job) -> None:
        done.append(job.id)

    for _ in range(5):
        queue.enqueue(db, "batch")
    db.commit()

    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()

    deadline = threading.Event()
    while len(done) < 5 and not deadline.wait(0.02):
        if thread.is_alive() is False:
            break
        if len(done) >= 5:
            break

    worker.request_stop()
    thread.join(timeout=10)

    assert not thread.is_alive(), "worker did not shut down"
    assert len(done) == 5


def test_stop_lets_the_current_job_finish(db: Session, worker: Worker) -> None:
    """Graceful shutdown: a deploy must not abandon work already in flight."""
    started = threading.Event()
    release = threading.Event()
    finished: list[int] = []

    @handlers.register("slow")
    def _slow(session: Session, job: Job) -> None:
        started.set()
        release.wait(timeout=10)
        finished.append(job.id)

    queue.enqueue(db, "slow")
    db.commit()

    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    assert started.wait(timeout=10)

    worker.request_stop()  # arrives mid-job
    release.set()
    thread.join(timeout=10)

    assert finished, "in-flight job was abandoned on shutdown"
    job = db.query(Job).one()
    db.refresh(job)
    assert job.status is JobStatus.SUCCEEDED


def test_reaper_is_rate_limited(db: Session, worker: Worker) -> None:
    """Sweeping much more often than the visibility timeout is pure load."""
    assert worker.maybe_reap() == 0
    worker._last_reap = 0.0  # force the interval to have elapsed
    assert worker.maybe_reap() == 0
