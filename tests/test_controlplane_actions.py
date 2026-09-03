"""Action semantics.

Actions are the only path that mutates control-plane state, so these tests cover the transition
itself: that it is pure, that it records why, and that it refuses what it cannot do.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from incident_copilot.controlplane.actions import (
    ActionError,
    NoAction,
    RestartService,
    RollbackDeploy,
    ScaleReplicas,
    SetConfigValue,
    ToggleFeatureFlag,
    apply_action,
)
from incident_copilot.controlplane.state import (
    ConfigValue,
    ControlPlaneState,
    FeatureFlag,
    LoadState,
    ServiceState,
)

CLOCK = datetime(2026, 1, 31, 9, 56, tzinfo=UTC)


@pytest.fixture
def state() -> ControlPlaneState:
    return ControlPlaneState(
        scenario="test",
        region="us-east",
        clock=CLOCK,
        services={
            "api": ServiceState(
                name="api",
                deployed_version="v2.0.0",
                previous_version="v1.9.0",
                replicas=6,
                min_replicas=2,
                max_replicas=12,
            ),
            "orphan": ServiceState(name="orphan", deployed_version="v1.0.0", replicas=1),
        },
        flags={"beta": FeatureFlag(key="beta", regions={"us-east": True, "eu-west": False})},
        config={"timeout_ms": ConfigValue(key="timeout_ms", value=1000, previous_value=2000)},
        load=LoadState(request_rate_rps=320),
    )


# -- purity -----------------------------------------------------------------


def test_apply_action_does_not_mutate_the_input(state: ControlPlaneState) -> None:
    """Before/after snapshots are only meaningful if the 'before' is untouched."""
    before = state.model_copy(deep=True)
    apply_action(state, SetConfigValue(key="timeout_ms", value=2000))
    assert state == before


# -- rollback_deploy --------------------------------------------------------


def test_rollback_uses_previous_version_by_default(state: ControlPlaneState) -> None:
    outcome = apply_action(state, RollbackDeploy(service="api"))
    svc = outcome.state.services["api"]
    assert svc.deployed_version == "v1.9.0"


def test_rollback_is_itself_reversible(state: ControlPlaneState) -> None:
    """The version rolled off becomes the new rollback target."""
    once = apply_action(state, RollbackDeploy(service="api")).state
    assert once.services["api"].previous_version == "v2.0.0"
    twice = apply_action(once, RollbackDeploy(service="api")).state
    assert twice.services["api"].deployed_version == "v2.0.0"


def test_rollback_without_a_previous_version_is_refused(state: ControlPlaneState) -> None:
    with pytest.raises(ActionError, match="no previous version"):
        apply_action(state, RollbackDeploy(service="orphan"))


def test_rollback_to_the_running_version_is_refused(state: ControlPlaneState) -> None:
    with pytest.raises(ActionError, match="already running"):
        apply_action(state, RollbackDeploy(service="api", to_version="v2.0.0"))


def test_unknown_service_is_refused(state: ControlPlaneState) -> None:
    with pytest.raises(ActionError, match="unknown service"):
        apply_action(state, RollbackDeploy(service="nope"))


# -- toggle_feature_flag ----------------------------------------------------


def test_flag_toggle_defaults_to_the_incident_region(state: ControlPlaneState) -> None:
    outcome = apply_action(state, ToggleFeatureFlag(flag="beta", enabled=False))
    flag = outcome.state.flags["beta"]
    assert flag.enabled_in("us-east") is False
    assert flag.enabled_in("eu-west") is False  # untouched, was already off


def test_flag_toggle_leaves_other_regions_alone(state: ControlPlaneState) -> None:
    outcome = apply_action(state, ToggleFeatureFlag(flag="beta", enabled=True, region="eu-west"))
    flag = outcome.state.flags["beta"]
    assert flag.enabled_in("eu-west") is True
    assert flag.enabled_in("us-east") is True


def test_redundant_flag_toggle_is_refused(state: ControlPlaneState) -> None:
    with pytest.raises(ActionError, match="already enabled"):
        apply_action(state, ToggleFeatureFlag(flag="beta", enabled=True))


def test_unknown_flag_is_refused(state: ControlPlaneState) -> None:
    with pytest.raises(ActionError, match="unknown feature flag"):
        apply_action(state, ToggleFeatureFlag(flag="nope", enabled=False))


# -- set_config_value -------------------------------------------------------


def test_config_change_records_the_previous_value(state: ControlPlaneState) -> None:
    outcome = apply_action(state, SetConfigValue(key="timeout_ms", value=2000))
    entry = outcome.state.config["timeout_ms"]
    assert (entry.value, entry.previous_value) == (2000, 1000)


def test_redundant_config_change_is_refused(state: ControlPlaneState) -> None:
    with pytest.raises(ActionError, match="already 1000"):
        apply_action(state, SetConfigValue(key="timeout_ms", value=1000))


def test_unknown_config_key_is_refused(state: ControlPlaneState) -> None:
    """Config keys are declared by the scenario; inventing one is a modelling error."""
    with pytest.raises(ActionError, match="unknown config key"):
        apply_action(state, SetConfigValue(key="nope", value=1))


# -- scale_replicas ---------------------------------------------------------


@pytest.mark.parametrize("replicas", [1, 13, 0, -1])
def test_scaling_outside_bounds_is_refused(state: ControlPlaneState, replicas: int) -> None:
    with pytest.raises(ActionError, match="replicas must be between"):
        apply_action(state, ScaleReplicas(service="api", replicas=replicas))


def test_scaling_within_bounds(state: ControlPlaneState) -> None:
    outcome = apply_action(state, ScaleReplicas(service="api", replicas=12))
    assert outcome.state.services["api"].replicas == 12


def test_scaling_to_the_current_count_is_refused(state: ControlPlaneState) -> None:
    with pytest.raises(ActionError, match="already has 6"):
        apply_action(state, ScaleReplicas(service="api", replicas=6))


# -- restart_service --------------------------------------------------------


def test_restart_stamps_the_clock_not_wall_time(state: ControlPlaneState) -> None:
    """Uptime-based faults must be driven by scenario time so tests stay deterministic."""
    outcome = apply_action(state, RestartService(service="api"))
    assert outcome.state.services["api"].last_restart == CLOCK


# -- change log -------------------------------------------------------------


def test_every_mutation_appends_exactly_one_change(state: ControlPlaneState) -> None:
    outcome = apply_action(state, SetConfigValue(key="timeout_ms", value=2000), actor="alice")
    assert len(outcome.state.changes) == len(state.changes) + 1
    entry = outcome.state.changes[-1]
    assert entry.kind == "config"
    assert entry.actor == "alice"
    assert entry.at == CLOCK
    assert "1000 → 2000" in entry.summary


def test_changes_accumulate_across_actions(state: ControlPlaneState) -> None:
    after_one = apply_action(state, SetConfigValue(key="timeout_ms", value=2000)).state
    after_two = apply_action(after_one, RestartService(service="api")).state
    assert [c.kind for c in after_two.changes] == ["config", "restart"]


def test_no_action_records_nothing(state: ControlPlaneState) -> None:
    outcome = apply_action(state, NoAction(reason="within SLO"))
    assert outcome.change is None
    assert outcome.state == state
    assert "within SLO" in outcome.summary
