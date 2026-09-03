"""The remediation action catalogue.

Every action is a pure state transition: `apply_action(state, action)` returns a new state plus
a change-log entry and a human-readable summary. Nothing mutates in place, so an execution can
record an exact before/after snapshot and a failed verification can be attributed to a specific
transition.

This module is deliberately the *only* place state changes. The agent's tool surface (phase 4)
contains none of it — actions reach the control plane solely through an approved proposal.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from incident_copilot.controlplane.state import (
    ChangeEntry,
    ConfigScalar,
    ConfigValue,
    ControlPlaneState,
)


class ActionError(Exception):
    """An action could not be applied to the current state.

    Raised for a target that does not exist, a parameter outside its allowed range, or a
    precondition that does not hold (rolling back a service with no previous version). These
    are expected outcomes of a model-proposed action, not bugs — the caller reports them back
    into the incident thread.
    """


# ---------------------------------------------------------------------------
# Action definitions
# ---------------------------------------------------------------------------


class RollbackDeploy(BaseModel):
    kind: Literal["rollback_deploy"] = "rollback_deploy"
    service: str
    to_version: str | None = Field(
        default=None, description="Defaults to the service's previous_version."
    )

    def describe(self) -> str:
        target = self.to_version or "the previous version"
        return f"Roll back {self.service} to {target}"


class ToggleFeatureFlag(BaseModel):
    kind: Literal["toggle_feature_flag"] = "toggle_feature_flag"
    flag: str
    enabled: bool
    region: str | None = Field(default=None, description="Defaults to the incident's region.")

    def describe(self) -> str:
        verb = "Enable" if self.enabled else "Disable"
        where = self.region or "the affected region"
        return f"{verb} flag {self.flag} in {where}"


class SetConfigValue(BaseModel):
    kind: Literal["set_config_value"] = "set_config_value"
    key: str
    value: ConfigScalar

    def describe(self) -> str:
        return f"Set {self.key} to {self.value}"


class ScaleReplicas(BaseModel):
    kind: Literal["scale_replicas"] = "scale_replicas"
    service: str
    replicas: int

    def describe(self) -> str:
        return f"Scale {self.service} to {self.replicas} replicas"


class RestartService(BaseModel):
    kind: Literal["restart_service"] = "restart_service"
    service: str

    def describe(self) -> str:
        return f"Restart {self.service}"


class NoAction(BaseModel):
    """An explicit decision not to act.

    A first-class action rather than an empty proposal list, so that "we looked and this needs
    no remediation" is recorded, auditable, and verifiable like any other decision.
    """

    kind: Literal["no_action"] = "no_action"
    reason: str = "No remediation required."

    def describe(self) -> str:
        return f"Take no action — {self.reason}"


Action = Annotated[
    RollbackDeploy | ToggleFeatureFlag | SetConfigValue | ScaleReplicas | RestartService | NoAction,
    Field(discriminator="kind"),
]

ACTION_KINDS: tuple[str, ...] = (
    "rollback_deploy",
    "toggle_feature_flag",
    "set_config_value",
    "scale_replicas",
    "restart_service",
    "no_action",
)


class ActionOutcome(BaseModel):
    """Result of applying one action."""

    state: ControlPlaneState
    change: ChangeEntry | None = Field(
        default=None, description="None for no_action, which records no change."
    )
    summary: str


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _rollback_deploy(state: ControlPlaneState, action: RollbackDeploy, actor: str) -> ActionOutcome:
    svc = state.service(action.service)
    if svc is None:
        raise ActionError(f"unknown service '{action.service}'")

    target = action.to_version or svc.previous_version
    if target is None:
        raise ActionError(f"{action.service} has no previous version to roll back to")
    if target == svc.deployed_version:
        raise ActionError(f"{action.service} is already running {target}")

    # The version being rolled off becomes the new rollback target, so a rollback is itself
    # reversible — a second rollback returns to where you started.
    updated = svc.model_copy(
        update={"deployed_version": target, "previous_version": svc.deployed_version}
    )
    summary = f"Rolled back {action.service} {svc.deployed_version} → {target}"
    return ActionOutcome(
        state=state.model_copy(update={"services": {**state.services, svc.name: updated}}),
        change=ChangeEntry(at=state.clock, kind="deploy", summary=summary, actor=actor),
        summary=summary,
    )


def _toggle_feature_flag(
    state: ControlPlaneState, action: ToggleFeatureFlag, actor: str
) -> ActionOutcome:
    flag = state.flags.get(action.flag)
    if flag is None:
        raise ActionError(f"unknown feature flag '{action.flag}'")

    region = action.region or state.region
    if flag.enabled_in(region) == action.enabled:
        raise ActionError(
            f"flag '{action.flag}' is already {'enabled' if action.enabled else 'disabled'} "
            f"in {region}"
        )

    updated = flag.model_copy(update={"regions": {**flag.regions, region: action.enabled}})
    verb = "Enabled" if action.enabled else "Disabled"
    summary = f"{verb} flag {action.flag} in {region}"
    return ActionOutcome(
        state=state.model_copy(update={"flags": {**state.flags, flag.key: updated}}),
        change=ChangeEntry(at=state.clock, kind="feature_flag", summary=summary, actor=actor),
        summary=summary,
    )


def _set_config_value(
    state: ControlPlaneState, action: SetConfigValue, actor: str
) -> ActionOutcome:
    existing = state.config.get(action.key)
    if existing is None:
        raise ActionError(f"unknown config key '{action.key}'")
    if existing.value == action.value:
        raise ActionError(f"config '{action.key}' is already {action.value}")

    updated = ConfigValue(key=action.key, value=action.value, previous_value=existing.value)
    summary = f"Set {action.key} {existing.value} → {action.value}"
    return ActionOutcome(
        state=state.model_copy(update={"config": {**state.config, action.key: updated}}),
        change=ChangeEntry(at=state.clock, kind="config", summary=summary, actor=actor),
        summary=summary,
    )


def _scale_replicas(state: ControlPlaneState, action: ScaleReplicas, actor: str) -> ActionOutcome:
    svc = state.service(action.service)
    if svc is None:
        raise ActionError(f"unknown service '{action.service}'")
    if not svc.min_replicas <= action.replicas <= svc.max_replicas:
        raise ActionError(
            f"replicas must be between {svc.min_replicas} and {svc.max_replicas}, "
            f"got {action.replicas}"
        )
    if action.replicas == svc.replicas:
        raise ActionError(f"{action.service} already has {action.replicas} replicas")

    updated = svc.model_copy(update={"replicas": action.replicas})
    summary = f"Scaled {action.service} {svc.replicas} → {action.replicas} replicas"
    return ActionOutcome(
        state=state.model_copy(update={"services": {**state.services, svc.name: updated}}),
        change=ChangeEntry(at=state.clock, kind="scale", summary=summary, actor=actor),
        summary=summary,
    )


def _restart_service(state: ControlPlaneState, action: RestartService, actor: str) -> ActionOutcome:
    svc = state.service(action.service)
    if svc is None:
        raise ActionError(f"unknown service '{action.service}'")

    updated = svc.model_copy(update={"last_restart": state.clock})
    summary = f"Restarted {action.service}"
    return ActionOutcome(
        state=state.model_copy(update={"services": {**state.services, svc.name: updated}}),
        change=ChangeEntry(at=state.clock, kind="restart", summary=summary, actor=actor),
        summary=summary,
    )


def apply_action(
    state: ControlPlaneState, action: Action, actor: str = "incident-copilot"
) -> ActionOutcome:
    """Apply one action, returning the resulting state.

    Raises `ActionError` when the action cannot be applied. The state passed in is never
    modified.
    """
    outcome: ActionOutcome
    match action:
        case RollbackDeploy():
            outcome = _rollback_deploy(state, action, actor)
        case ToggleFeatureFlag():
            outcome = _toggle_feature_flag(state, action, actor)
        case SetConfigValue():
            outcome = _set_config_value(state, action, actor)
        case ScaleReplicas():
            outcome = _scale_replicas(state, action, actor)
        case RestartService():
            outcome = _restart_service(state, action, actor)
        case NoAction():
            return ActionOutcome(state=state, change=None, summary=action.describe())

    # Appending here rather than in each handler keeps the change log and the state change
    # atomic — there is no path that mutates state without recording why.
    assert outcome.change is not None
    return outcome.model_copy(update={"state": outcome.state.with_changes(outcome.change)})
