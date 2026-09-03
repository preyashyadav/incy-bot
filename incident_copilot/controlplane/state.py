"""Control-plane state: the mutable world the copilot observes and remediates.

This is the single source of truth for the simulated environment. Metrics and logs are never
stored — they are *derived* from this state (see `simulator.py`), which is what makes an
approved remediation visibly change the numbers instead of replaying a canned response.

State transitions happen only through `actions.apply_action`, which returns a new state rather
than mutating in place, so every execution can record an exact before/after snapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ConfigScalar = bool | int | float | str


class ServiceState(BaseModel):
    """One deployable service."""

    name: str
    deployed_version: str
    previous_version: str | None = Field(
        default=None,
        description="Last-known-good version. `rollback_deploy` targets this when no version "
        "is given; a service with no previous version cannot be rolled back.",
    )
    replicas: int = 3
    min_replicas: int = 1
    max_replicas: int = 32
    last_restart: datetime | None = None

    model_config = {"frozen": True}


class FeatureFlag(BaseModel):
    """A flag with independent per-region values.

    Region granularity matters: the most common real remediation is disabling a flag in the one
    region that is on fire while leaving it on elsewhere, and a global boolean cannot express
    that distinction.
    """

    key: str
    regions: dict[str, bool] = Field(default_factory=dict)

    model_config = {"frozen": True}

    def enabled_in(self, region: str) -> bool:
        return self.regions.get(region, False)

    def any_enabled(self) -> bool:
        return any(self.regions.values())


class ConfigValue(BaseModel):
    """A tunable configuration value, with the value it was changed from."""

    key: str
    value: ConfigScalar
    previous_value: ConfigScalar | None = None

    model_config = {"frozen": True}


class LoadState(BaseModel):
    """Inbound traffic. Independent of any fault — surges are a legitimate, non-incident cause
    of elevated metrics, which is what the `noisy_neighbor` scenario exists to test."""

    request_rate_rps: float

    model_config = {"frozen": True}


class ChangeEntry(BaseModel):
    """One entry in the change log.

    Seeded from the scenario's history and appended to by every executed action, so the change
    log the agent reads always reflects remediations that have already been applied.
    """

    at: datetime
    kind: Literal["deploy", "feature_flag", "config", "scale", "restart"]
    summary: str
    actor: str = "unknown"

    model_config = {"frozen": True}


class ControlPlaneState(BaseModel):
    """The complete world state for one scenario instance."""

    scenario: str
    region: str
    clock: datetime = Field(
        description="The scenario's notion of 'now'. Fixed rather than wall-clock so that "
        "derived logs and change timestamps are deterministic and tests are stable."
    )
    services: dict[str, ServiceState] = Field(default_factory=dict)
    flags: dict[str, FeatureFlag] = Field(default_factory=dict)
    config: dict[str, ConfigValue] = Field(default_factory=dict)
    load: LoadState
    changes: list[ChangeEntry] = Field(default_factory=list)

    model_config = {"frozen": True}

    # -- lookups used by fault conditions ---------------------------------

    def service(self, name: str) -> ServiceState | None:
        return self.services.get(name)

    def flag_enabled(self, key: str, region: str | None = None) -> bool:
        flag = self.flags.get(key)
        if flag is None:
            return False
        return flag.enabled_in(region or self.region)

    def config_value(self, key: str) -> ConfigScalar | None:
        entry = self.config.get(key)
        return None if entry is None else entry.value

    def config_number(self, key: str) -> float | None:
        """Numeric view of a config value, or None if absent or non-numeric.

        `bool` is excluded explicitly: it is a subclass of `int` in Python, and silently
        comparing a feature toggle against a numeric threshold would be a real bug.
        """
        value = self.config_value(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    def minutes_since_restart(self, service_name: str) -> float | None:
        svc = self.service(service_name)
        if svc is None or svc.last_restart is None:
            return None
        return (self.clock - svc.last_restart).total_seconds() / 60.0

    def with_changes(self, *entries: ChangeEntry) -> ControlPlaneState:
        return self.model_copy(update={"changes": [*self.changes, *entries]})
