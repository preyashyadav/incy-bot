"""FastAPI application.

Phase 0 exposes only health endpoints. The Slack Bolt adapter mounts here in phase 5, and the
scenario/admin routes arrive in phase 1.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import text

from incident_copilot.api import dashboard, scenarios
from incident_copilot.config import get_settings
from incident_copilot.db.session import get_engine

logger = logging.getLogger(__name__)


class Health(BaseModel):
    status: Literal["ok"]
    environment: str
    slack_mode: str


class Readiness(BaseModel):
    status: Literal["ready", "degraded"]
    database: Literal["up", "down"]
    knowledge_base: Literal["indexed", "empty", "unknown"] = "unknown"
    detail: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Schema is owned by Alembic, not by create_all() — the previous version created tables on
    # startup, which silently diverges from migrations the moment a column changes.
    yield


def _mount_slack(app: FastAPI) -> None:
    """Serve Slack over HTTP when configured to.

    Only in `http` mode: under Socket Mode the Bolt app runs in its own process holding a
    WebSocket, and mounting it here as well would give one workspace two competing consumers of
    the same interactions.

    Missing credentials are logged rather than raised. The API is useful without Slack — the
    scenario and control-plane routes are the whole demo surface — and refusing to boot over an
    unset token would make that impossible.
    """
    settings = get_settings()
    if settings.slack_mode != "http":
        logger.info("slack_mode=%s; not mounting the HTTP endpoint", settings.slack_mode)
        return
    try:
        from incident_copilot.slack.app import build_asgi_handler

        handler = build_asgi_handler(settings)
    except Exception as exc:
        logger.warning("slack HTTP endpoint not mounted: %s", exc)
        return

    @app.post("/slack/events", include_in_schema=False)
    async def slack_events(request: Request) -> Any:
        """Bolt verifies the signature and timestamp of every request that reaches it."""
        return await handler.handle(request)

    logger.info("slack HTTP endpoint mounted at /slack/events")


app = FastAPI(
    title="Incident Copilot",
    version="0.1.0",
    description="Slack-native incident response: investigate, propose, approve, remediate, verify.",
    lifespan=lifespan,
)

app.include_router(scenarios.router)
app.include_router(dashboard.router)
_mount_slack(app)


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
            indexed = conn.execute(text("SELECT EXISTS (SELECT 1 FROM kb_chunks)")).scalar()
    except Exception as exc:
        return Readiness(status="degraded", database="down", detail=type(exc).__name__)

    if not indexed:
        # An empty index does not break anything — retrieval simply returns nothing and the
        # agent falls back on telemetry alone. That silent degradation is worse than a loud
        # failure: investigations still look successful while the knowledge base contributes
        # nothing at all.
        return Readiness(
            status="degraded",
            database="up",
            knowledge_base="empty",
            detail="knowledge base is empty; run `make index`",
        )
    return Readiness(status="ready", database="up", knowledge_base="indexed")
