"""API health endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from incident_copilot.api.main import app

client = TestClient(app)


def test_healthz_is_up_without_a_database() -> None:
    """Liveness must not depend on Postgres, or a DB blip restarts healthy pods."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_reports_database_state() -> None:
    """Readiness reflects the database either way — it never raises."""
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] in {"up", "down"}
    assert body["status"] == ("ready" if body["database"] == "up" else "degraded")


def test_readyz_flags_an_empty_knowledge_base(db: object) -> None:
    """An empty index degrades silently otherwise.

    Retrieval returns nothing, the agent falls back on telemetry, and the investigation still
    looks successful — while the knowledge base contributes nothing. This happened for real
    before the check existed.
    """
    from incident_copilot.retrieval.index import reindex

    body = client.get("/readyz").json()
    assert body["status"] == "degraded"
    assert body["knowledge_base"] == "empty"
    assert "make index" in body["detail"]

    reindex(db)  # type: ignore[arg-type]
    db.commit()  # type: ignore[attr-defined]

    body = client.get("/readyz").json()
    assert body["status"] == "ready"
    assert body["knowledge_base"] == "indexed"
