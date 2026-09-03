"""Control-plane HTTP surface."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from incident_copilot.api.main import app
from incident_copilot.controlplane.store import get_store

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_control_plane() -> Iterator[None]:
    """Reset between tests — the in-memory store is process-wide shared state."""
    get_store().reset_all()
    yield
    get_store().reset_all()


def test_list_scenarios() -> None:
    response = client.get("/scenarios")
    assert response.status_code == 200
    keys = {row["key"] for row in response.json()}
    assert "payments_gateway_timeout" in keys
    assert "noisy_neighbor_traffic_surge" in keys


def test_unknown_scenario_is_404() -> None:
    assert client.get("/scenarios/nope/metrics").status_code == 404


def test_metrics_reflect_the_active_fault() -> None:
    metrics = client.get("/scenarios/payments_gateway_timeout/metrics").json()
    assert metrics["error_rate"] == pytest.approx(0.124)
    assert metrics["p95_latency_ms"] == pytest.approx(1450.0)
    assert metrics["upstream_timeout_rate"] == pytest.approx(0.098)


def test_health_names_the_breaches() -> None:
    health = client.get("/scenarios/payments_gateway_timeout/health").json()
    assert health["healthy"] is False
    assert health["active_faults"] == ["gateway_timeout_too_low"]
    assert {b["metric"] for b in health["breaches"]} >= {"error_rate", "upstream_timeout_rate"}


def test_changes_are_newest_first() -> None:
    changes = client.get("/scenarios/payments_gateway_timeout/changes").json()
    timestamps = [c["at"] for c in changes]
    assert timestamps == sorted(timestamps, reverse=True)


def test_logs_are_rendered_from_live_state() -> None:
    logs = client.get("/scenarios/payments_gateway_timeout/logs").json()
    joined = " ".join(line["message"] for line in logs["lines"])
    assert "1000ms" in joined  # substituted from config, not hardcoded


def _apply(key: str, action: dict[str, Any]) -> Any:
    return client.post(f"/scenarios/{key}/actions", json=action)


def test_applying_the_fix_recovers_and_persists() -> None:
    response = _apply(
        "payments_gateway_timeout",
        {"kind": "set_config_value", "key": "gateway_timeout_ms", "value": 2000},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["health_before"]["healthy"] is False
    assert body["health_after"]["healthy"] is True
    assert body["metrics_after"]["error_rate"] == pytest.approx(0.002)
    assert body["change"]["kind"] == "config"

    # The mutation stuck — a later read sees the recovered system.
    assert client.get("/scenarios/payments_gateway_timeout/health").json()["healthy"] is True


def test_logs_change_after_remediation() -> None:
    before = client.get("/scenarios/payments_gateway_timeout/logs").json()
    _apply(
        "payments_gateway_timeout",
        {"kind": "set_config_value", "key": "gateway_timeout_ms", "value": 2000},
    )
    after = client.get("/scenarios/payments_gateway_timeout/logs").json()
    assert len(after["lines"]) < len(before["lines"])
    assert "CircuitBreakerOpenException" not in " ".join(x["message"] for x in after["lines"])


def test_wrong_fix_is_accepted_but_does_not_recover() -> None:
    body = _apply(
        "payments_gateway_timeout",
        {"kind": "scale_replicas", "service": "payments-api", "replicas": 12},
    ).json()
    assert body["health_after"]["healthy"] is False
    # It is a real action with real effects — CPU moved, the fault did not.
    assert body["metrics_after"]["cpu_utilization_percent"] == pytest.approx(22.5)
    assert body["metrics_after"]["error_rate"] == pytest.approx(0.124)


def test_conflicting_action_is_409_not_500() -> None:
    action = {"kind": "set_config_value", "key": "gateway_timeout_ms", "value": 2000}
    assert _apply("payments_gateway_timeout", action).status_code == 200
    conflict = _apply("payments_gateway_timeout", action)
    assert conflict.status_code == 409
    assert "already 2000" in conflict.json()["detail"]


def test_malformed_action_is_422() -> None:
    """A schema error is distinct from a state conflict."""
    assert _apply("payments_gateway_timeout", {"kind": "not_a_real_action"}).status_code == 422
    assert _apply("payments_gateway_timeout", {"kind": "restart_service"}).status_code == 422


def test_reset_restores_the_starting_point() -> None:
    _apply(
        "payments_gateway_timeout",
        {"kind": "set_config_value", "key": "gateway_timeout_ms", "value": 2000},
    )
    assert client.get("/scenarios/payments_gateway_timeout/health").json()["healthy"] is True

    assert client.post("/scenarios/payments_gateway_timeout/reset").status_code == 200
    assert client.get("/scenarios/payments_gateway_timeout/health").json()["healthy"] is False


def test_reset_all_clears_every_scenario() -> None:
    _apply("checkout_memory_leak", {"kind": "restart_service", "service": "checkout-api"})
    assert client.get("/scenarios/checkout_memory_leak/health").json()["healthy"] is True

    assert client.post("/scenarios/reset").status_code == 204
    assert client.get("/scenarios/checkout_memory_leak/health").json()["healthy"] is False


def test_no_action_records_no_change() -> None:
    body = _apply(
        "noisy_neighbor_traffic_surge", {"kind": "no_action", "reason": "within SLO"}
    ).json()
    assert body["change"] is None
    assert body["health_before"]["healthy"] is True
    assert body["health_after"]["healthy"] is True
