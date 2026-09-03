"""Derives observable telemetry from control-plane state.

Nothing here is stored. Every read recomputes metrics and logs from the current state and the
scenario's fault rules, which is why remediating a fault makes the numbers move without any
recovery-specific code path.

The derivation has two parts:

1. **Load and capacity** — a deterministic function of traffic and replica count. This is what
   makes `scale_replicas` a *real* action: it genuinely lowers CPU and latency. It just does not
   fix a fault whose condition is a feature flag, which is exactly the lesson the
   `payments_gateway_timeout` scenario teaches.
2. **Fault effects** — additive deltas from whichever rules are currently active.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from incident_copilot.controlplane.faults import LogTemplate, active_rules
from incident_copilot.controlplane.state import ControlPlaneState

if TYPE_CHECKING:
    from incident_copilot.controlplane.scenarios import Scenario

# How sharply latency responds to utilisation above the baseline. Chosen so that a 2x
# over-subscription roughly doubles p95 — steep enough to be visible, gentle enough that a
# modest surge stays inside a typical SLO.
_LATENCY_LOAD_SENSITIVITY = 1.0

_SUBSTITUTION = re.compile(r"\{([a-z_]+)(?:\.([A-Za-z0-9_.\-]+))?\}")


class Metrics(BaseModel):
    """A metrics snapshot. Field names match what the agent's tools expose."""

    window: str
    error_rate: float
    p95_latency_ms: float
    upstream_timeout_rate: float
    request_rate_rps: float
    availability: float
    cpu_utilization_percent: float
    memory_usage_mb: float


class LogLine(BaseModel):
    timestamp: str
    level: str
    service: str
    message: str

    def render(self) -> str:
        return f"{self.timestamp} {self.level} {self.service} {self.message}"


class Logs(BaseModel):
    window: str
    lines: list[LogLine] = Field(default_factory=list)


class SLO(BaseModel):
    """The thresholds that define "recovered".

    Verification (phase 6) compares derived metrics against these. Declaring them per scenario
    rather than globally lets a latency regression be a SEV2 that never breaches an error-rate
    threshold.
    """

    max_error_rate: float = 0.01
    max_p95_latency_ms: float = 800.0
    max_upstream_timeout_rate: float = 0.01
    min_availability: float = 0.995
    max_cpu_utilization_percent: float = 85.0
    max_memory_usage_mb: float = 1600.0


class SLOBreach(BaseModel):
    metric: str
    observed: float
    threshold: float
    comparison: str

    def describe(self) -> str:
        return f"{self.metric} {self.observed:g} {self.comparison} {self.threshold:g}"


class HealthAssessment(BaseModel):
    healthy: bool
    breaches: list[SLOBreach] = Field(default_factory=list)
    active_faults: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        if self.healthy:
            return "All metrics within SLO."
        return "; ".join(breach.describe() for breach in self.breaches)


class Baseline(BaseModel):
    """What the system looks like with no fault active, at baseline load and replica count."""

    window: str = "last_15_minutes"
    error_rate: float = 0.002
    p95_latency_ms: float = 240.0
    upstream_timeout_rate: float = 0.0
    availability: float = 0.999
    cpu_utilization_percent: float = 45.0
    memory_usage_mb: float = 512.0
    request_rate_rps: float = Field(
        default=320.0, description="Traffic the baseline metrics were measured at."
    )
    replicas: int = Field(
        default=6,
        description="Replica count the baseline was measured at. Explicit rather than read "
        "from initial state so a scenario can start already-autoscaled.",
    )


def utilisation_factor(baseline: Baseline, state: ControlPlaneState, service: str) -> float:
    """Per-replica load relative to baseline.

    1.0 means each replica is carrying exactly what it carried at baseline. Doubling traffic
    doubles it; doubling replicas halves it.
    """
    svc = state.service(service)
    replicas = svc.replicas if svc is not None else baseline.replicas
    if replicas <= 0 or baseline.request_rate_rps <= 0:
        return 1.0
    load_ratio = state.load.request_rate_rps / baseline.request_rate_rps
    capacity_ratio = baseline.replicas / replicas
    return load_ratio * capacity_ratio


def compute_metrics(scenario: Scenario, state: ControlPlaneState) -> Metrics:
    """Derive the current metrics snapshot."""
    baseline = scenario.baseline
    factor = utilisation_factor(baseline, state, scenario.service)

    # Latency degrades only above baseline utilisation; spare capacity does not make a service
    # faster than its floor.
    latency_pressure = 1.0 + _LATENCY_LOAD_SENSITIVITY * max(0.0, factor - 1.0)

    error_rate = baseline.error_rate
    p95 = baseline.p95_latency_ms * latency_pressure
    upstream_timeout_rate = baseline.upstream_timeout_rate
    availability = baseline.availability
    cpu = baseline.cpu_utilization_percent * factor
    memory = baseline.memory_usage_mb

    for rule in active_rules(scenario.faults, state):
        error_rate += rule.effect.error_rate
        p95 += rule.effect.p95_latency_ms
        upstream_timeout_rate += rule.effect.upstream_timeout_rate
        availability += rule.effect.availability
        cpu += rule.effect.cpu_utilization_percent
        memory += rule.effect.memory_usage_mb

    return Metrics(
        window=baseline.window,
        error_rate=round(_clamp(error_rate, 0.0, 1.0), 4),
        p95_latency_ms=round(max(0.0, p95), 1),
        upstream_timeout_rate=round(_clamp(upstream_timeout_rate, 0.0, 1.0), 4),
        request_rate_rps=round(state.load.request_rate_rps, 1),
        availability=round(_clamp(availability, 0.0, 1.0), 5),
        cpu_utilization_percent=round(_clamp(cpu, 0.0, 100.0), 1),
        memory_usage_mb=round(max(0.0, memory), 1),
    )


def compute_logs(scenario: Scenario, state: ControlPlaneState) -> Logs:
    """Render log lines for the currently active faults, plus the scenario's baseline lines.

    Timestamps count backwards from the scenario clock at a fixed interval. Determinism is
    deliberate: the agent's evidence must be reproducible across runs, and the record/replay
    tests in phase 4 depend on it.
    """
    templates: list[LogTemplate] = list(scenario.baseline_logs)
    for rule in active_rules(scenario.faults, state):
        templates.extend(rule.logs)

    expanded: list[LogTemplate] = []
    for template in templates:
        expanded.extend([template] * max(1, template.count))

    lines: list[LogLine] = []
    for index, template in enumerate(expanded):
        # Oldest first, so the rendered window reads chronologically.
        offset = timedelta(seconds=17 * (len(expanded) - index))
        lines.append(
            LogLine(
                timestamp=(state.clock - offset).isoformat().replace("+00:00", "Z"),
                level=template.level,
                service=template.service,
                message=_substitute(template.message, state),
            )
        )
    return Logs(window=scenario.baseline.window, lines=lines)


def assess_health(scenario: Scenario, state: ControlPlaneState) -> HealthAssessment:
    """Compare derived metrics against the scenario SLO."""
    metrics = compute_metrics(scenario, state)
    slo = scenario.slo
    checks: list[tuple[str, float, float, str]] = [
        ("error_rate", metrics.error_rate, slo.max_error_rate, ">"),
        ("p95_latency_ms", metrics.p95_latency_ms, slo.max_p95_latency_ms, ">"),
        (
            "upstream_timeout_rate",
            metrics.upstream_timeout_rate,
            slo.max_upstream_timeout_rate,
            ">",
        ),
        (
            "cpu_utilization_percent",
            metrics.cpu_utilization_percent,
            slo.max_cpu_utilization_percent,
            ">",
        ),
        ("memory_usage_mb", metrics.memory_usage_mb, slo.max_memory_usage_mb, ">"),
        ("availability", metrics.availability, slo.min_availability, "<"),
    ]

    breaches = [
        SLOBreach(metric=name, observed=observed, threshold=threshold, comparison=comparison)
        for name, observed, threshold, comparison in checks
        if (observed > threshold if comparison == ">" else observed < threshold)
    ]
    return HealthAssessment(
        healthy=not breaches,
        breaches=breaches,
        active_faults=[rule.id for rule in active_rules(scenario.faults, state)],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _substitute(message: str, state: ControlPlaneState) -> str:
    """Resolve `{region}`, `{config.key}`, `{version.service}` and `{replicas.service}`.

    An unresolvable reference is left verbatim rather than raising: a malformed log template in
    a scenario file should not take down evidence gathering mid-incident.
    """

    def replace(match: re.Match[str]) -> str:
        namespace, name = match.group(1), match.group(2)
        if namespace == "region" and name is None:
            return state.region
        if name is None:
            return match.group(0)
        if namespace == "config":
            value = state.config_value(name)
            return match.group(0) if value is None else str(value)
        if namespace == "version":
            svc = state.service(name)
            return match.group(0) if svc is None else svc.deployed_version
        if namespace == "replicas":
            svc = state.service(name)
            return match.group(0) if svc is None else str(svc.replicas)
        return match.group(0)

    return _SUBSTITUTION.sub(replace, message)
