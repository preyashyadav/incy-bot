"""Scenario packs: the incident catalogue.

A scenario is pure data — initial state, baseline telemetry, fault rules, change history, the
alert that fires, and the SLO that defines recovery. Adding an incident type means adding a YAML
file, not writing code.

`ground_truth` is the one field the agent must never see. It records the remediation that
actually resolves the scenario and exists so the end-to-end tests can assert the copilot reached
the right answer. `Scenario.for_agent()` returns the view that omits it.
"""

from __future__ import annotations

import functools
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from incident_copilot.controlplane.actions import Action
from incident_copilot.controlplane.faults import FaultRule, LogTemplate
from incident_copilot.controlplane.simulator import SLO, Baseline
from incident_copilot.controlplane.state import (
    ChangeEntry,
    ConfigValue,
    ControlPlaneState,
    FeatureFlag,
    LoadState,
    ServiceState,
)

SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "scenarios"

Severity = Literal["SEV1", "SEV2", "SEV3"]


class ScenarioError(Exception):
    """A scenario pack is missing or malformed."""


class Alert(BaseModel):
    """What arrives in Slack. Deliberately thin — the alert states symptoms, never causes.

    If the alert named the root cause there would be nothing to investigate, and the demo would
    be testing formatting rather than reasoning.
    """

    signal: str
    summary: str
    impact: str
    detected_at: datetime


class GroundTruth(BaseModel):
    """The correct remediation. Test-only — never exposed to the agent.

    Declares the concrete action rather than just its kind, so the end-to-end tests can apply it
    directly and assert the scenario actually recovers. A scenario whose ground truth does not
    resolve it is a broken scenario, and `test_scenarios.py` enforces exactly that.
    """

    action: Action
    rationale: str
    also_acceptable: list[Action] = Field(
        default_factory=list,
        description="Other actions that legitimately resolve the scenario.",
    )
    requires_followup: bool = Field(
        default=False,
        description="True when the correct action mitigates without fixing the underlying "
        "cause, and a follow-up note is expected.",
    )


class InitialState(BaseModel):
    """Serialised form of the control plane's starting point."""

    region: str = "us-east"
    clock: datetime
    services: list[ServiceState] = Field(default_factory=list)
    flags: list[FeatureFlag] = Field(default_factory=list)
    config: list[ConfigValue] = Field(default_factory=list)
    request_rate_rps: float = 320.0
    changes: list[ChangeEntry] = Field(default_factory=list)


class Scenario(BaseModel):
    """One incident scenario."""

    key: str
    title: str
    description: str
    service: str = Field(description="The service whose capacity drives load-derived metrics.")
    expected_severity: Severity
    alert: Alert
    initial: InitialState
    baseline: Baseline = Field(default_factory=Baseline)
    baseline_logs: list[LogTemplate] = Field(default_factory=list)
    faults: list[FaultRule] = Field(default_factory=list)
    slo: SLO = Field(default_factory=SLO)
    ground_truth: GroundTruth

    def initial_state(self) -> ControlPlaneState:
        """Build a fresh control-plane state. Called on every reset."""
        return ControlPlaneState(
            scenario=self.key,
            region=self.initial.region,
            clock=self.initial.clock,
            services={svc.name: svc for svc in self.initial.services},
            flags={flag.key: flag for flag in self.initial.flags},
            config={entry.key: entry for entry in self.initial.config},
            load=LoadState(request_rate_rps=self.initial.request_rate_rps),
            changes=list(self.initial.changes),
        )

    def for_agent(self) -> dict[str, Any]:
        """The scenario as the agent may see it — ground truth and fault rules removed.

        Fault rules are withheld as well as ground truth: they encode the causal model, so
        exposing them would hand over the answer just as directly.
        """
        return {
            "key": self.key,
            "title": self.title,
            "service": self.service,
            "alert": self.alert.model_dump(mode="json"),
        }


class ScenarioRegistry:
    """Loads and caches scenario packs from disk."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or SCENARIOS_DIR
        self._cache: dict[str, Scenario] | None = None

    def _load_all(self) -> dict[str, Scenario]:
        if self._cache is not None:
            return self._cache
        if not self.directory.is_dir():
            raise ScenarioError(f"scenario directory not found: {self.directory}")

        scenarios: dict[str, Scenario] = {}
        for path in sorted(self.directory.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ScenarioError(f"{path.name}: expected a YAML mapping")
            try:
                scenario = Scenario.model_validate(raw)
            except ValidationError as exc:
                raise ScenarioError(f"{path.name}: {exc}") from exc

            if scenario.key != path.stem:
                raise ScenarioError(
                    f"{path.name}: key '{scenario.key}' must match the filename stem"
                )
            if scenario.service not in {svc.name for svc in scenario.initial.services}:
                raise ScenarioError(
                    f"{path.name}: service '{scenario.service}' is not in initial.services"
                )
            scenarios[scenario.key] = scenario

        if not scenarios:
            raise ScenarioError(f"no scenario packs found in {self.directory}")
        self._cache = scenarios
        return scenarios

    def keys(self) -> list[str]:
        return sorted(self._load_all())

    def all(self) -> list[Scenario]:
        return [self._load_all()[key] for key in self.keys()]

    def get(self, key: str) -> Scenario:
        try:
            return self._load_all()[key]
        except KeyError:
            raise ScenarioError(
                f"unknown scenario '{key}'. Available: {', '.join(self.keys())}"
            ) from None


@functools.lru_cache(maxsize=1)
def get_registry() -> ScenarioRegistry:
    return ScenarioRegistry()
