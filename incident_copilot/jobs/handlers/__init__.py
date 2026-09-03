"""Job handler registry.

A handler is a function taking `(session, job)` and returning nothing. Registration is by job
kind, so the worker never imports domain modules directly and a handler can be added in a later
phase without touching the worker loop.

Handlers must be **idempotent**. Delivery is at-least-once: a worker killed between doing the
work and marking the job complete will run the same job again.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from incident_copilot.db.models import Job

JobHandler = Callable[[Session, Job], None]

_HANDLERS: dict[str, JobHandler] = {}


class UnknownJobKind(Exception):
    """No handler is registered for this job's kind."""


def register(kind: str) -> Callable[[JobHandler], JobHandler]:
    """Register a handler for a job kind."""

    def decorator(fn: JobHandler) -> JobHandler:
        if kind in _HANDLERS:
            raise RuntimeError(f"handler for job kind '{kind}' is already registered")
        _HANDLERS[kind] = fn
        return fn

    return decorator


def get_handler(kind: str) -> JobHandler:
    try:
        return _HANDLERS[kind]
    except KeyError:
        raise UnknownJobKind(
            f"no handler registered for job kind '{kind}'. Registered: "
            f"{', '.join(sorted(_HANDLERS)) or '(none)'}"
        ) from None


def registered_kinds() -> list[str]:
    return sorted(_HANDLERS)


def unregister(kind: str) -> None:
    """Remove a handler. Test-support only — the registry is process-global."""
    _HANDLERS.pop(kind, None)
