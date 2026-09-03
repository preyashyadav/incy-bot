"""Shared test fixtures.

Database-backed tests need a real Postgres — `SKIP LOCKED`, partial indexes, `ON CONFLICT`, and
JSONB have no meaningful SQLite equivalent, and a fake would test the fake rather than the query
that ships.

**Tests run against their own database.** `DATABASE_URL`'s database name gets a `_test` suffix,
and that database is created on first use. Without this the suite truncates the database a
developer is actively demoing against: `make check` silently destroys running incidents, which is
exactly what happened once before this fixture existed.

Redirecting the environment variable — rather than only the fixtures — is what makes it airtight.
Application code reaches the database through `session_scope()`, which resolves the global engine
from settings; a fixture-only override would leave every Slack handler and job handler writing to
the developer's database while the fixtures politely used another.

If Postgres is unreachable the DB tests skip locally (so `pytest` still works before `make
db-up`) but **fail in CI**. A silently-green CI that skipped every concurrency test would be
worse than no CI at all.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from incident_copilot.config import Settings, get_settings
from incident_copilot.db.models import Base
from incident_copilot.db.session import get_engine, get_sessionmaker

TEST_DB_SUFFIX = "_test"

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


def _with_database(url: str, name: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{name}"))


def _database_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _ensure_database(admin_url: str, name: str) -> None:
    """Create the test database if it does not exist.

    CREATE DATABASE cannot run inside a transaction, hence the AUTOCOMMIT isolation level.
    """
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar_one_or_none()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


def _reachable(url: str) -> bool:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def isolated_database() -> Iterator[str]:
    """Point the whole process at a dedicated test database, for the whole session."""
    configured = str(Settings().database_url)  # type: ignore[call-arg]
    source_name = _database_name(configured)

    # Idempotent: honour an already-suffixed URL (CI may set one explicitly).
    test_name = (
        source_name if source_name.endswith(TEST_DB_SUFFIX) else f"{source_name}{TEST_DB_SUFFIX}"
    )
    test_url = _with_database(configured, test_name)

    if not _reachable(_with_database(configured, "postgres")):
        message = f"Postgres unreachable at {configured}. Run `make db-up`."
        if os.getenv("ENVIRONMENT") == "ci" or os.getenv("CI"):
            pytest.fail(message)  # never let CI pass by skipping these
        pytest.skip(message, allow_module_level=True)

    _ensure_database(_with_database(configured, "postgres"), test_name)

    # Redirect the application's own engine, not just the fixtures below.
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

    engine = get_engine()
    with engine.begin() as conn:
        # pgvector is created by the baseline migration; create_all does not know about it.
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)

    yield test_url

    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture(scope="session")
def engine(isolated_database: str) -> Engine:
    """The same engine the application uses, now pointed at the test database."""
    return get_engine()


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
