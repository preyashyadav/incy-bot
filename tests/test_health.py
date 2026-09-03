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
