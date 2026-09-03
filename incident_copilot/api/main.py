"""FastAPI application.

Phase 0 exposes only health endpoints. The Slack Bolt adapter mounts here in phase 5, and the
scenario/admin routes arrive in phase 1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import text

from incident_copilot.api import scenarios
from incident_copilot.config import get_settings
from incident_copilot.db.session import get_engine


class Health(BaseModel):
    status: Literal["ok"]
    environment: str
    slack_mode: str


class Readiness(BaseModel):
    status: Literal["ready", "degraded"]
    database: Literal["up", "down"]
    detail: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Schema is owned by Alembic, not by create_all() — the previous version created tables on
    # startup, which silently diverges from migrations the moment a column changes.
    yield


app = FastAPI(
    title="Incident Copilot",
    version="0.1.0",
    description="Slack-native incident response: investigate, propose, approve, remediate, verify.",
    lifespan=lifespan,
)

app.include_router(scenarios.router)


@app.get("/healthz", response_model=Health)
def healthz() -> Health:
    """Liveness: the process is up. Deliberately does not touch the database."""
    settings = get_settings()
    return Health(
        status="ok",
        environment=settings.environment,
        slack_mode=settings.slack_mode,
    )


@app.get("/readyz", response_model=Readiness)
def readyz() -> Readiness:
    """Readiness: dependencies are reachable. This is the one a load balancer should poll."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        return Readiness(status="degraded", database="down", detail=type(exc).__name__)
    return Readiness(status="ready", database="up")
