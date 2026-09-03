"""Scenario and control-plane routes.

This is the operator and debugging surface: inspect a scenario, read its derived telemetry, apply
an action directly, reset it. The Slack path (phase 5) drives the same control plane through an
approval gate — these routes exist so the simulator can be exercised and demonstrated without
Slack in the loop.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel

from incident_copilot.controlplane.actions import Action, ActionError, apply_action
from incident_copilot.controlplane.scenarios import (
    Scenario,
    ScenarioError,
    ScenarioRegistry,
    get_registry,
)
from incident_copilot.controlplane.simulator import (
    HealthAssessment,
    Logs,
    Metrics,
    assess_health,
    compute_logs,
    compute_metrics,
)
from incident_copilot.controlplane.state import ChangeEntry, ControlPlaneState
from incident_copilot.controlplane.store import ControlPlaneStore, get_store

router = APIRouter(prefix="/scenarios", tags=["control-plane"])

RegistryDep = Annotated[ScenarioRegistry, Depends(get_registry)]
StoreDep = Annotated[ControlPlaneStore, Depends(get_store)]


class ScenarioSummary(BaseModel):
    key: str
    title: str
    description: str
    service: str
    expected_severity: str


class ActionResult(BaseModel):
    applied: str
    change: ChangeEntry | None
    health_before: HealthAssessment
    health_after: HealthAssessment
    metrics_after: Metrics

    @property
    def recovered(self) -> bool:
        return self.health_after.healthy


def _scenario(registry: ScenarioRegistry, key: str) -> Scenario:
    try:
        return registry.get(key)
    except ScenarioError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("", response_model=list[ScenarioSummary])
def list_scenarios(registry: RegistryDep) -> list[ScenarioSummary]:
    return [
        ScenarioSummary(
            key=s.key,
            title=s.title,
            description=" ".join(s.description.split()),
            service=s.service,
            expected_severity=s.expected_severity,
        )
        for s in registry.all()
    ]


@router.get("/{key}/state", response_model=ControlPlaneState)
def get_state(key: str, registry: RegistryDep, store: StoreDep) -> ControlPlaneState:
    _scenario(registry, key)
    return store.get(key)


@router.get("/{key}/metrics", response_model=Metrics)
def get_metrics(key: str, registry: RegistryDep, store: StoreDep) -> Metrics:
    scenario = _scenario(registry, key)
    return compute_metrics(scenario, store.get(key))


@router.get("/{key}/logs", response_model=Logs)
def get_logs(key: str, registry: RegistryDep, store: StoreDep) -> Logs:
    scenario = _scenario(registry, key)
    return compute_logs(scenario, store.get(key))


@router.get("/{key}/changes", response_model=list[ChangeEntry])
def get_changes(key: str, registry: RegistryDep, store: StoreDep) -> list[ChangeEntry]:
    _scenario(registry, key)
    # Most recent first — the change most likely to be causal is the one that just happened.
    return sorted(store.get(key).changes, key=lambda c: c.at, reverse=True)


@router.get("/{key}/health", response_model=HealthAssessment)
def get_health(key: str, registry: RegistryDep, store: StoreDep) -> HealthAssessment:
    scenario = _scenario(registry, key)
    return assess_health(scenario, store.get(key))


@router.post("/{key}/actions", response_model=ActionResult)
def execute_action(
    key: str,
    registry: RegistryDep,
    store: StoreDep,
    action: Annotated[Action, Body()],
    actor: str = "operator",
) -> ActionResult:
    """Apply an action directly. The Slack path routes through an approval gate instead."""
    scenario = _scenario(registry, key)
    before = store.get(key)
    health_before = assess_health(scenario, before)

    try:
        outcome = apply_action(before, action, actor=actor)
    except ActionError as exc:
        # 409: the action is well-formed but conflicts with current state (already applied,
        # nothing to roll back to). A schema problem would already have been a 422.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    store.put(outcome.state)
    return ActionResult(
        applied=outcome.summary,
        change=outcome.change,
        health_before=health_before,
        health_after=assess_health(scenario, outcome.state),
        metrics_after=compute_metrics(scenario, outcome.state),
    )


@router.post("/{key}/reset", response_model=ControlPlaneState)
def reset_scenario(key: str, registry: RegistryDep, store: StoreDep) -> ControlPlaneState:
    _scenario(registry, key)
    return store.reset(key)


@router.post("/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_all(store: StoreDep) -> None:
    store.reset_all()
