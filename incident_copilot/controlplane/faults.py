"""Declarative fault rules.

A scenario does not hardcode "the error rate is 12.4%". It declares the *conditions* under which
a fault is active and the effect that fault has on metrics. The simulator evaluates the rules
against current state on every read.

That indirection is the whole trick: remediate the condition and the effect disappears on its
own, with no scenario-specific code path for recovery — and remediating the *wrong* thing leaves
the condition true, so verification fails honestly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from incident_copilot.controlplane.state import ControlPlaneState


class FaultCondition(BaseModel):
    """Predicates over control-plane state. All populated predicates must hold (AND).

    Every field is optional; an empty condition is always active, which is how a scenario
    expresses an unconditional baseline degradation.
    """

    flag_enabled: list[str] = Field(
        default_factory=list,
        description="Flag keys that must be enabled in the incident's region.",
    )
    flag_disabled: list[str] = Field(default_factory=list)
    config_lt: dict[str, float] = Field(default_factory=dict, description="config[key] < threshold")
    config_gt: dict[str, float] = Field(default_factory=dict)
    config_eq: dict[str, bool | int | float | str] = Field(default_factory=dict)
    version_in: dict[str, list[str]] = Field(
        default_factory=dict,
        description="service -> versions considered bad. Active while deployed_version matches.",
    )
    replicas_lt: dict[str, int] = Field(default_factory=dict)
    minutes_since_restart_gt: dict[str, float] = Field(
        default_factory=dict,
        description="service -> minutes. Models faults that accumulate after a restart, such "
        "as a memory leak. Never active for a service that has never recorded a restart.",
    )
    rps_gt: float | None = None

    def is_active(self, state: ControlPlaneState) -> bool:
        for key in self.flag_enabled:
            if not state.flag_enabled(key):
                return False
        for key in self.flag_disabled:
            if state.flag_enabled(key):
                return False

        for key, threshold in self.config_lt.items():
            value = state.config_number(key)
            if value is None or value >= threshold:
                return False
        for key, threshold in self.config_gt.items():
            value = state.config_number(key)
            if value is None or value <= threshold:
                return False
        for key, expected in self.config_eq.items():
            if state.config_value(key) != expected:
                return False

        for service_name, bad_versions in self.version_in.items():
            svc = state.service(service_name)
            if svc is None or svc.deployed_version not in bad_versions:
                return False

        for service_name, threshold_replicas in self.replicas_lt.items():
            svc = state.service(service_name)
            if svc is None or svc.replicas >= threshold_replicas:
                return False

        for service_name, threshold_minutes in self.minutes_since_restart_gt.items():
            elapsed = state.minutes_since_restart(service_name)
            if elapsed is None or elapsed <= threshold_minutes:
                return False

        return not (self.rps_gt is not None and state.load.request_rate_rps <= self.rps_gt)


class MetricEffect(BaseModel):
    """Additive deltas applied to baseline metrics while a fault is active.

    Additive rather than absolute so that overlapping faults compose, and so a scenario's
    baseline stays readable as "what healthy looks like".
    """

    error_rate: float = 0.0
    p95_latency_ms: float = 0.0
    upstream_timeout_rate: float = 0.0
    availability: float = 0.0
    cpu_utilization_percent: float = 0.0
    memory_usage_mb: float = 0.0


class LogTemplate(BaseModel):
    """A log line emitted while the owning fault is active.

    `message` supports `{config.key}`, `{service.name.version}`, `{replicas.name}` and
    `{region}` substitution so lines reflect live state — a log that still quotes the old
    timeout value after a config change would give the agent contradictory evidence.
    """

    level: str = "ERROR"
    service: str
    message: str
    count: int = 1


class FaultRule(BaseModel):
    """A named failure mode: when it is active, and what it does to the system."""

    id: str
    description: str
    when: FaultCondition = Field(default_factory=FaultCondition)
    effect: MetricEffect = Field(default_factory=MetricEffect)
    logs: list[LogTemplate] = Field(default_factory=list)

    def is_active(self, state: ControlPlaneState) -> bool:
        return self.when.is_active(state)


def active_rules(rules: list[FaultRule], state: ControlPlaneState) -> list[FaultRule]:
    return [rule for rule in rules if rule.is_active(state)]
