"""The worker process.

Claims jobs from Postgres, runs the registered handler, and marks the result. Runs as a separate
process from the API so that a slow investigation cannot consume the capacity that Slack's
three-second acknowledgement budget depends on.

Shutdown is graceful: SIGTERM/SIGINT stop the loop from picking up new work but let the job in
flight finish. A hard kill is also safe — the reaper returns the stranded job to the queue.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import UTC, datetime
from types import FrameType

from incident_copilot.config import Settings, get_settings
from incident_copilot.db.models import Job, JobStatus
from incident_copilot.db.session import session_scope
from incident_copilot.jobs import queue
from incident_copilot.jobs.handlers import UnknownJobKind, get_handler

logger = logging.getLogger(__name__)

# How often to sweep for jobs stranded by a dead worker, as a multiple of the visibility timeout.
# Sweeping much more often than the timeout only adds load; the sweep itself is cheap and
# indexed.
REAP_INTERVAL_FACTOR = 0.5


def make_worker_id() -> str:
    """Identifies the holder of a lock. Host and PID make a stalled worker traceable."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        worker_id: str | None = None,
        kinds: list[str] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.worker_id = worker_id or make_worker_id()
        self.kinds = kinds
        self._stop = threading.Event()
        self._last_reap = 0.0
        self.processed = 0

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: FrameType | None) -> None:
            logger.info("worker %s received %s, finishing current job", self.worker_id, signum)
            self.request_stop()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    # -- work --------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim and run at most one job. Returns True if a job was processed.

        The claim commits before the handler runs. Holding the claim transaction open for the
        duration of the work would keep a row lock for the length of an LLM call, block the
        reaper, and risk exhausting the connection pool.
        """
        with session_scope() as session:
            job = queue.claim(session, self.worker_id, kinds=self.kinds)
            if job is None:
                return False
            job_id, kind, attempts = job.id, job.kind, job.attempts

        logger.info("job %s (%s) claimed by %s, attempt %s", job_id, kind, self.worker_id, attempts)
        started = time.monotonic()

        try:
            with session_scope() as session:
                claimed = session.get(Job, job_id)
                assert claimed is not None
                get_handler(kind)(session, claimed)
                queue.complete(session, claimed)
        except UnknownJobKind as exc:
            # A missing handler is a deploy problem, not a transient fault. Retrying it three
            # times just delays the alert, so bury it immediately.
            logger.error("job %s: %s", job_id, exc)
            self._bury(job_id, str(exc))
        except Exception as exc:
            logger.exception("job %s (%s) failed", job_id, kind)
            self._record_failure(job_id, f"{type(exc).__name__}: {exc}")
        else:
            logger.info("job %s (%s) done in %.2fs", job_id, kind, time.monotonic() - started)

        self.processed += 1
        return True

    def _record_failure(self, job_id: int, error: str) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            status = queue.fail(session, job, error)
            if status is JobStatus.DEAD:
                logger.error("job %s exhausted its retries and is dead: %s", job_id, error)

    def _bury(self, job_id: int, error: str) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.attempts = job.max_attempts
            queue.fail(session, job, error)

    def maybe_reap(self) -> int:
        interval = self.settings.worker_visibility_timeout_seconds * REAP_INTERVAL_FACTOR
        now = time.monotonic()
        if now - self._last_reap < interval:
            return 0
        self._last_reap = now
        with session_scope() as session:
            reclaimed = queue.reap_stalled(session, self.settings.worker_visibility_timeout_seconds)
        if reclaimed:
            logger.warning("reclaimed %s stalled job(s)", reclaimed)
        return reclaimed

    def run_forever(self) -> None:
        logger.info("worker %s started at %s", self.worker_id, datetime.now(UTC).isoformat())
        while not self._stop.is_set():
            self.maybe_reap()
            try:
                did_work = self.run_once()
            except Exception:
                # The loop itself must survive anything the queue layer throws — a database
                # blip should pause the worker, not terminate it.
                logger.exception("worker loop error; backing off")
                self._stop.wait(self.settings.worker_poll_interval_seconds)
                continue
            if not did_work:
                # Poll interval only applies when idle; a busy queue is drained without pause.
                self._stop.wait(self.settings.worker_poll_interval_seconds)
        logger.info("worker %s stopped after %s job(s)", self.worker_id, self.processed)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    worker = Worker(settings)
    worker.install_signal_handlers()
    worker.run_forever()


if __name__ == "__main__":
    main()
