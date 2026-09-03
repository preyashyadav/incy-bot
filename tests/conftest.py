"""Shared test fixtures.

Database-backed tests need a real Postgres — `SKIP LOCKED`, partial indexes, `ON CONFLICT`, and
JSONB have no meaningful SQLite equivalent, and a fake would test the fake rather than the query
that ships.

If Postgres is unreachable the DB tests skip locally (so `pytest` still works before `make
db-up`) but **fail in CI**. A silently-green CI that skipped every concurrency test would be
worse than no CI at all.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from incident_copilot.config import get_settings
from incident_copilot.db.models import Base

# Written by tests, truncated between them. Ordered so that a plain TRUNCATE … CASCADE is
# unambiguous about what it is clearing.
_TABLES = [
    "action_executions",
    "approval_tokens",
    "proposals",
    "incident_events",
    "jobs",
    "incidents",
    "slack_deliveries",
    "control_plane_state",
    "kb_chunks",
]


def _database_available(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    settings = get_settings()
    eng = create_engine(str(settings.database_url), future=True, pool_pre_ping=True)

    if not _database_available(eng):
        message = (
            f"Postgres unreachable at {settings.database_url}. Run `make db-up && make migrate`."
        )
        if os.getenv("ENVIRONMENT") == "ci" or os.getenv("CI"):
            pytest.fail(message)  # never let CI pass by skipping these
        pytest.skip(message, allow_module_level=True)

    # The schema under test is the one migrations produce; create_all here is only a safety net
    # for a fresh database that has not been migrated yet.
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A clean database and one session, per test."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """For tests that need genuinely concurrent connections, not one shared session."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
