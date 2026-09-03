"""Telemetry derivation.

The central property under test: metrics are a function of state. Change the state, the numbers
change; change nothing, the numbers are identical.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from incident_copilot.controlplane.actions import ScaleReplicas, apply_action
from incident_copilot.controlplane.faults import (
    FaultCondition,
    FaultRule,
    LogTemplate,
    MetricEffect,
)
from incident_copilot.controlplane.scenarios import (
    Alert,
    GroundTruth,
    InitialState,
    Scenario,
)
from incident_copilot.controlplane.simulator import (
    SLO,
    Baseline,
    assess_health,
    compute_logs,
    compute_metrics,
    utilisation_factor,
)
from incident_copilot.controlplane.state import (
    ConfigValue,
    ControlPlaneState,
    FeatureFlag,
    LoadState,
    ServiceState,
)

CLOCK = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def build_scenario(**overrides: object) -> Scenario:
    defaults: dict[str, object] = {
        "key": "test",
        "title": "Test scenario",
        "description": "fixture",
        "service": "api",
        "expected_severity": "SEV2",
        "alert": Alert(signal="error_rate_spike", summary="s", impact="i", detected_at=CLOCK),
        "initial": InitialState(
            clock=CLOCK,
            services=[ServiceState(name="api", deployed_version="v1", replicas=4)],
            request_rate_rps=100.0,
        ),
        "baseline": Baseline(
            error_rate=0.001,
            p95_latency_ms=200.0,
            cpu_utilization_percent=40.0,
            request_rate_rps=100.0,
            replicas=4,
        ),
        "slo": SLO(),
        "ground_truth": GroundTruth(action={"kind": "no_action"}, rationale="n/a"),  # type: ignore[arg-type]
    }
    defaults.update(overrides)
    return Scenario.model_validate(defaults)


# -- load and capacity ------------------------------------------------------


def test_baseline_state_sits_at_utilisation_one() -> None:
    scenario = build_scenario()
    assert utilisation_factor(scenario.baseline, scenario.initial_state(), "api") == 1.0


def test_doubling_traffic_doubles_utilisation() -> None:
    scenario = build_scenario()
    state = scenario.initial_state().model_copy(update={"load": LoadState(request_rate_rps=200.0)})
    assert utilisation_factor(scenario.baseline, state, "api") == 2.0


def test_doubling_replicas_halves_utilisation() -> None:
    scenario = build_scenario()
    state = apply_action(scenario.initial_state(), ScaleReplicas(service="api", replicas=8)).state
    assert utilisation_factor(scenario.baseline, state, "api") == 0.5


def test_spare_capacity_does_not_beat_the_latency_floor() -> None:
    """Over-provisioning must not report latency below baseline — that would be fiction."""
    scenario = build_scenario()
    state = apply_action(scenario.initial_state(), ScaleReplicas(service="api", replicas=16)).state
    assert compute_metrics(scenario, state).p95_latency_ms == scenario.baseline.p95_latency_ms


def test_load_raises_latency_and_cpu() -> None:
    scenario = build_scenario()
    state = scenario.initial_state().model_copy(update={"load": LoadState(request_rate_rps=200.0)})
    metrics = compute_metrics(scenario, state)
    assert metrics.p95_latency_ms == 400.0  # 200 * (1 + 1.0 * 1.0)
    assert metrics.cpu_utilization_percent == 80.0  # 40 * 2.0


# -- fault rules ------------------------------------------------------------


def test_inactive_rule_contributes_nothing() -> None:
    scenario = build_scenario(
        faults=[
            FaultRule(
                id="dormant",
                description="never fires here",
                when=FaultCondition(flag_enabled=["absent"]),
                effect=MetricEffect(error_rate=0.5),
            )
        ]
    )
    metrics = compute_metrics(scenario, scenario.initial_state())
    assert metrics.error_rate == scenario.baseline.error_rate


def test_active_rules_compose_additively() -> None:
    scenario = build_scenario(
        faults=[
            FaultRule(id="a", description="", effect=MetricEffect(error_rate=0.05)),
            FaultRule(id="b", description="", effect=MetricEffect(error_rate=0.02)),
        ]
    )
    metrics = compute_metrics(scenario, scenario.initial_state())
    assert metrics.error_rate == pytest.approx(0.071)


def test_empty_condition_is_always_active() -> None:
    rule = FaultRule(id="always", description="")
    scenario = build_scenario(faults=[rule])
    assert rule.is_active(scenario.initial_state())


def test_metrics_are_clamped_to_physical_ranges() -> None:
    scenario = build_scenario(
        faults=[
            FaultRule(
                id="absurd",
                description="",
                effect=MetricEffect(
                    error_rate=99.0, availability=-99.0, cpu_utilization_percent=500.0
                ),
            )
        ]
    )
    metrics = compute_metrics(scenario, scenario.initial_state())
    assert metrics.error_rate == 1.0
    assert metrics.availability == 0.0
    assert metrics.cpu_utilization_percent == 100.0


# -- fault conditions -------------------------------------------------------


def _state(**overrides: object) -> ControlPlaneState:
    base: dict[str, object] = {
        "scenario": "test",
        "region": "us-east",
        "clock": CLOCK,
        "services": {"api": ServiceState(name="api", deployed_version="v2", replicas=4)},
        "flags": {"f": FeatureFlag(key="f", regions={"us-east": True, "eu-west": False})},
        "config": {"n": ConfigValue(key="n", value=1000), "on": ConfigValue(key="on", value=True)},
        "load": LoadState(request_rate_rps=100.0),
    }
    base.update(overrides)
    return ControlPlaneState.model_validate(base)


def test_flag_condition_is_region_scoped() -> None:
    condition = FaultCondition(flag_enabled=["f"])
    assert condition.is_active(_state()) is True
    assert condition.is_active(_state(region="eu-west")) is False


def test_all_predicates_must_hold() -> None:
    condition = FaultCondition(flag_enabled=["f"], config_lt={"n": 1500})
    assert condition.is_active(_state()) is True
    # Same flag, but the numeric predicate no longer holds.
    assert condition.is_active(_state(config={"n": ConfigValue(key="n", value=2000)})) is False


def test_boolean_config_is_not_treated_as_a_number() -> None:
    """bool subclasses int in Python; comparing a toggle to a threshold would be a real bug."""
    assert FaultCondition(config_lt={"on": 5}).is_active(_state()) is False


def test_missing_config_key_makes_a_numeric_condition_false() -> None:
    assert FaultCondition(config_lt={"absent": 5}).is_active(_state()) is False


def test_version_condition() -> None:
    assert FaultCondition(version_in={"api": ["v2"]}).is_active(_state()) is True
    assert FaultCondition(version_in={"api": ["v1"]}).is_active(_state()) is False


def test_restart_condition_is_false_without_a_recorded_restart() -> None:
    """A service that has never restarted must not be treated as infinitely stale."""
    assert FaultCondition(minutes_since_restart_gt={"api": 1}).is_active(_state()) is False


def test_restart_condition_uses_scenario_clock() -> None:
    stale = ServiceState(
        name="api", deployed_version="v2", replicas=4, last_restart=CLOCK - timedelta(hours=6)
    )
    state = _state(services={"api": stale})
    assert FaultCondition(minutes_since_restart_gt={"api": 240}).is_active(state) is True
    assert FaultCondition(minutes_since_restart_gt={"api": 400}).is_active(state) is False


def test_rps_condition() -> None:
    assert FaultCondition(rps_gt=50).is_active(_state()) is True
    assert FaultCondition(rps_gt=500).is_active(_state()) is False


# -- logs -------------------------------------------------------------------


def test_logs_include_baseline_and_active_faults_only() -> None:
    scenario = build_scenario(
        baseline_logs=[LogTemplate(level="INFO", service="api", message="baseline")],
        faults=[
            FaultRule(
                id="on",
                description="",
                logs=[LogTemplate(level="ERROR", service="api", message="active")],
            ),
            FaultRule(
                id="off",
                description="",
                when=FaultCondition(flag_enabled=["absent"]),
                logs=[LogTemplate(level="ERROR", service="api", message="dormant")],
            ),
        ],
    )
    messages = [line.message for line in compute_logs(scenario, scenario.initial_state()).lines]
    assert "baseline" in messages
    assert "active" in messages
    assert "dormant" not in messages


def test_log_count_expands_templates() -> None:
    scenario = build_scenario(
        baseline_logs=[LogTemplate(level="WARN", service="api", message="x", count=3)]
    )
    assert len(compute_logs(scenario, scenario.initial_state()).lines) == 3


def test_logs_are_deterministic_and_chronological() -> None:
    """Record/replay agent tests in phase 4 depend on byte-identical evidence across runs."""
    scenario = build_scenario(
        baseline_logs=[LogTemplate(level="INFO", service="api", message="a", count=4)]
    )
    state = scenario.initial_state()
    first = compute_logs(scenario, state)
    assert first == compute_logs(scenario, state)
    timestamps = [line.timestamp for line in first.lines]
    assert timestamps == sorted(timestamps)


def test_log_substitution_reflects_live_state() -> None:
    """A log still quoting the pre-remediation value would be contradictory evidence."""
    scenario = build_scenario(
        baseline_logs=[
            LogTemplate(
                level="ERROR",
                service="api",
                message=(
                    "timeout={config.n} version={version.api} "
                    "replicas={replicas.api} region={region}"
                ),
            )
        ],
        initial=InitialState(
            clock=CLOCK,
            services=[ServiceState(name="api", deployed_version="v7", replicas=4)],
            config=[ConfigValue(key="n", value=1000)],
            request_rate_rps=100.0,
        ),
    )
    message = compute_logs(scenario, scenario.initial_state()).lines[0].message
    assert message == "timeout=1000 version=v7 replicas=4 region=us-east"


def test_unresolvable_substitution_is_left_verbatim() -> None:
    """A malformed template must not break evidence gathering mid-incident."""
    scenario = build_scenario(
        baseline_logs=[LogTemplate(level="INFO", service="api", message="{config.missing} {bogus}")]
    )
    assert compute_logs(scenario, scenario.initial_state()).lines[0].message == (
        "{config.missing} {bogus}"
    )


# -- health -----------------------------------------------------------------


def test_healthy_when_all_metrics_are_inside_slo() -> None:
    assessment = assess_health(scenario := build_scenario(), scenario.initial_state())
    assert assessment.healthy
    assert assessment.breaches == []


def test_breaches_name_the_offending_metric() -> None:
    scenario = build_scenario(
        faults=[FaultRule(id="bad", description="", effect=MetricEffect(error_rate=0.5))]
    )
    assessment = assess_health(scenario, scenario.initial_state())
    assert not assessment.healthy
    assert [b.metric for b in assessment.breaches] == ["error_rate"]
    assert assessment.active_faults == ["bad"]


def test_availability_breach_uses_a_minimum_not_a_maximum() -> None:
    scenario = build_scenario(
        faults=[FaultRule(id="down", description="", effect=MetricEffect(availability=-0.5))]
    )
    assessment = assess_health(scenario, scenario.initial_state())
    breach = next(b for b in assessment.breaches if b.metric == "availability")
    assert breach.comparison == "<"
