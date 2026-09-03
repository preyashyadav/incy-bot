"""Scenario pack integrity.

These are the tests that hold the premise of the whole project up. If a scenario's declared
remediation does not actually resolve it, or an unrelated action silently does, then phase 6's
verification step is meaningless and the demo is theatre.

Every scenario in `scenarios/` is covered automatically — adding a pack adds coverage.
"""

from __future__ import annotations

from typing import Any

import pytest

from incident_copilot.controlplane.actions import (
    Action,
    ActionError,
    NoAction,
    RestartService,
    RollbackDeploy,
    ScaleReplicas,
    apply_action,
)
from incident_copilot.controlplane.scenarios import (
    Scenario,
    ScenarioError,
    ScenarioRegistry,
    get_registry,
)
from incident_copilot.controlplane.simulator import assess_health, compute_logs, compute_metrics

REGISTRY = get_registry()
ALL_SCENARIOS = REGISTRY.all()
SCENARIO_IDS = [s.key for s in ALL_SCENARIOS]


def scenarios_needing_remediation() -> list[Scenario]:
    """Every pack that starts unhealthy — all but `noisy_neighbor`."""
    return [s for s in ALL_SCENARIOS if not assess_health(s, s.initial_state()).healthy]


# -- registry ---------------------------------------------------------------


def test_the_catalogue_is_not_empty() -> None:
    assert len(ALL_SCENARIOS) >= 5


def test_unknown_scenario_lists_the_available_ones() -> None:
    with pytest.raises(ScenarioError, match="Available:"):
        REGISTRY.get("does-not-exist")


def test_missing_directory_is_reported_clearly(tmp_path: Any) -> None:
    with pytest.raises(ScenarioError, match="scenario directory not found"):
        ScenarioRegistry(tmp_path / "absent").keys()


def test_empty_directory_is_reported_clearly(tmp_path: Any) -> None:
    with pytest.raises(ScenarioError, match="no scenario packs found"):
        ScenarioRegistry(tmp_path).keys()


# -- per-scenario invariants ------------------------------------------------


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=SCENARIO_IDS)
def test_initial_state_is_reproducible(scenario: Scenario) -> None:
    """Reset must be exact, or a demo's second run differs from its first."""
    assert scenario.initial_state() == scenario.initial_state()


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=SCENARIO_IDS)
def test_alert_does_not_leak_the_cause(scenario: Scenario) -> None:
    """The alert states symptoms. If it named the fix there would be nothing to investigate."""
    text = f"{scenario.alert.summary} {scenario.alert.impact}".lower()
    for fault in scenario.faults:
        assert fault.id.replace("_", " ") not in text


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=SCENARIO_IDS)
def test_agent_view_withholds_ground_truth_and_faults(scenario: Scenario) -> None:
    """The single most important information boundary in the project."""
    view = scenario.for_agent()
    serialised = str(view)
    assert "ground_truth" not in view
    assert "faults" not in view
    assert scenario.ground_truth.rationale[:40] not in serialised
    for fault in scenario.faults:
        assert fault.id not in serialised


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=SCENARIO_IDS)
def test_evidence_is_derivable_without_error(scenario: Scenario) -> None:
    state = scenario.initial_state()
    assert compute_metrics(scenario, state).request_rate_rps > 0
    assert compute_logs(scenario, state).lines


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=SCENARIO_IDS)
def test_every_fault_rule_is_reachable(scenario: Scenario) -> None:
    """A rule that no state can activate is dead weight in the pack.

    Dormant-at-start is fine and intentional (`noisy_neighbor`), but the rule must at least be
    expressible — every service, flag, and config key it references has to exist.
    """
    state = scenario.initial_state()
    for rule in scenario.faults:
        for service in {
            *rule.when.version_in,
            *rule.when.replicas_lt,
            *rule.when.minutes_since_restart_gt,
        }:
            assert state.service(service) is not None, f"{rule.id}: unknown service {service}"
        for flag in [*rule.when.flag_enabled, *rule.when.flag_disabled]:
            assert flag in state.flags, f"{rule.id}: unknown flag {flag}"
        for key in {*rule.when.config_lt, *rule.when.config_gt, *rule.when.config_eq}:
            assert key in state.config, f"{rule.id}: unknown config key {key}"


# -- the central property ---------------------------------------------------


@pytest.mark.parametrize("scenario", scenarios_needing_remediation(), ids=lambda s: s.key)
def test_broken_scenarios_start_unhealthy_with_an_active_fault(scenario: Scenario) -> None:
    assessment = assess_health(scenario, scenario.initial_state())
    assert not assessment.healthy
    assert assessment.active_faults
    assert assessment.describe()


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=SCENARIO_IDS)
def test_ground_truth_action_resolves_the_scenario(scenario: Scenario) -> None:
    """The load-bearing test: the declared fix must actually work."""
    outcome = apply_action(scenario.initial_state(), scenario.ground_truth.action)
    assessment = assess_health(scenario, outcome.state)
    assert assessment.healthy, f"{scenario.key} not resolved: {assessment.describe()}"
    assert not assessment.active_faults


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=SCENARIO_IDS)
def test_alternative_accepted_actions_also_resolve_the_scenario(scenario: Scenario) -> None:
    for action in scenario.ground_truth.also_acceptable:
        outcome = apply_action(scenario.initial_state(), action)
        assert assess_health(scenario, outcome.state).healthy, (
            f"{scenario.key}: also_acceptable action {action.kind} does not resolve it"
        )


@pytest.mark.parametrize("scenario", scenarios_needing_remediation(), ids=lambda s: s.key)
def test_scaling_up_never_resolves_a_fault(scenario: Scenario) -> None:
    """Adding capacity is a real action with real effects — but it is not a root-cause fix.

    Skipped where scaling is itself the declared remedy; no current pack is in that position,
    and the guard keeps the test honest if one is added.
    """
    if isinstance(scenario.ground_truth.action, ScaleReplicas):
        pytest.skip("scaling is the declared remedy for this scenario")

    state = scenario.initial_state()
    svc = state.services[scenario.service]
    if svc.replicas >= svc.max_replicas:
        pytest.skip("already at max replicas")

    outcome = apply_action(state, ScaleReplicas(service=svc.name, replicas=svc.max_replicas))
    assert not assess_health(scenario, outcome.state).healthy


@pytest.mark.parametrize("scenario", scenarios_needing_remediation(), ids=lambda s: s.key)
def test_an_unrelated_action_does_not_resolve_the_fault(scenario: Scenario) -> None:
    """Guards against a scenario whose fault clears on any state change at all."""
    ground_truth = scenario.ground_truth.action
    state = scenario.initial_state()
    svc = state.services[scenario.service]

    candidates: list[Action] = []
    if not isinstance(ground_truth, RestartService):
        candidates.append(RestartService(service=svc.name))
    if not isinstance(ground_truth, RollbackDeploy) and svc.previous_version:
        candidates.append(RollbackDeploy(service=svc.name))

    assert candidates, f"{scenario.key}: no unrelated action available to test"
    for action in candidates:
        outcome = apply_action(state, action)
        assert not assess_health(scenario, outcome.state).healthy, (
            f"{scenario.key}: unrelated {action.kind} wrongly resolved the fault"
        )


# -- the scenario that must not be remediated -------------------------------


def test_noisy_neighbor_starts_healthy_and_wants_no_action() -> None:
    """An incident bot that always proposes a remediation is pattern-matching, not reasoning."""
    scenario = REGISTRY.get("noisy_neighbor_traffic_surge")
    assessment = assess_health(scenario, scenario.initial_state())
    assert assessment.healthy
    assert assessment.active_faults == []
    assert isinstance(scenario.ground_truth.action, NoAction)


def test_noisy_neighbor_is_elevated_but_within_slo() -> None:
    """The page was real — the metrics are genuinely raised, they just are not a breach."""
    scenario = REGISTRY.get("noisy_neighbor_traffic_surge")
    metrics = compute_metrics(scenario, scenario.initial_state())
    assert metrics.request_rate_rps > scenario.baseline.request_rate_rps * 2
    assert metrics.p95_latency_ms > scenario.baseline.p95_latency_ms
    assert metrics.p95_latency_ms < scenario.slo.max_p95_latency_ms


def test_scaling_down_during_the_surge_causes_an_incident() -> None:
    """The dormant rule makes 'do nothing' a real judgement rather than a trivial one."""
    scenario = REGISTRY.get("noisy_neighbor_traffic_surge")
    outcome = apply_action(scenario.initial_state(), ScaleReplicas(service="feed-api", replicas=6))
    assessment = assess_health(scenario, outcome.state)
    assert not assessment.healthy
    assert "undersized_for_surge" in assessment.active_faults


# -- follow-up semantics ----------------------------------------------------


def test_memory_leak_recurs_after_the_restart_wears_off() -> None:
    """`requires_followup` is a claim about the world; this asserts the world backs it up."""
    from datetime import timedelta

    scenario = REGISTRY.get("checkout_memory_leak")
    assert scenario.ground_truth.requires_followup

    restarted = apply_action(scenario.initial_state(), RestartService(service="checkout-api")).state
    assert assess_health(scenario, restarted).healthy

    # Advance the scenario clock past the degradation threshold; the leak returns untouched.
    later = restarted.model_copy(update={"clock": restarted.clock + timedelta(hours=5)})
    assert not assess_health(scenario, later).healthy


def test_scenarios_without_followup_stay_fixed() -> None:
    from datetime import timedelta

    for scenario in ALL_SCENARIOS:
        if scenario.ground_truth.requires_followup:
            continue
        fixed = apply_action(scenario.initial_state(), scenario.ground_truth.action).state
        later = fixed.model_copy(update={"clock": fixed.clock + timedelta(hours=12)})
        assert assess_health(scenario, later).healthy, f"{scenario.key} regressed over time"


# -- severity ---------------------------------------------------------------


def test_severity_spread_covers_the_range() -> None:
    """The catalogue must be able to distinguish an outage from a slow page."""
    severities = {s.expected_severity for s in ALL_SCENARIOS}
    assert {"SEV1", "SEV2", "SEV3"} <= severities


def test_latency_regression_is_not_an_error_rate_incident() -> None:
    """Degradation is not outage — this is what stops everything being classified SEV1."""
    scenario = REGISTRY.get("latency_regression_n_plus_one")
    assessment = assess_health(scenario, scenario.initial_state())
    breached = {b.metric for b in assessment.breaches}
    assert "p95_latency_ms" in breached
    assert "error_rate" not in breached
    assert scenario.expected_severity == "SEV2"


# -- refusals ---------------------------------------------------------------


def test_applying_the_ground_truth_twice_is_refused() -> None:
    """Idempotency at the action layer: a replayed approval must not double-apply."""
    scenario = REGISTRY.get("payments_gateway_timeout")
    once = apply_action(scenario.initial_state(), scenario.ground_truth.action).state
    with pytest.raises(ActionError):
        apply_action(once, scenario.ground_truth.action)
