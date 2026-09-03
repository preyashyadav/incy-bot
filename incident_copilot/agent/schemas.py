"""The contract between the model and the rest of the system.

`IncidentProposal` is produced by a structured-output call, so it is schema-valid by
construction — there is no JSON repair path, no `except JSONDecodeError`, and no partially
parsed proposal reaching the approval card.

`ProposedAction` is deliberately *flat* rather than the discriminated union from
`controlplane.actions`. Structured-output schemas are strictest and most portable when every
field is a plain optional scalar, and the conversion in `to_action()` gives a precise, reportable
error when the model omits a field its chosen action needs. Validating at the boundary is also
what lets an otherwise-good proposal survive one malformed action.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from incident_copilot.controlplane.actions import (
    Action,
    NoAction,
    RestartService,
    RollbackDeploy,
    ScaleReplicas,
    SetConfigValue,
    ToggleFeatureFlag,
)

ActionKind = Literal[
    "rollback_deploy",
    "toggle_feature_flag",
    "set_config_value",
    "scale_replicas",
    "restart_service",
    "no_action",
]
Confidence = Literal["low", "medium", "high"]
Severity = Literal["SEV1", "SEV2", "SEV3"]
Risk = Literal["low", "medium", "high"]


class InvalidProposedAction(Exception):
    """A proposed action is missing a field its kind requires."""


class ProposedAction(BaseModel):
    kind: ActionKind
    rationale: str = Field(description="Why this action addresses the hypothesised cause.")
    risk: Risk
    reversible: bool

    # Populated according to `kind`; see `to_action`.
    service: str | None = None
    flag: str | None = None
    enabled: bool | None = None
    region: str | None = None
    key: str | None = None
    value: str | int | float | bool | None = None
    replicas: int | None = None
    reason: str | None = None

    def to_action(self) -> Action:
        """Convert to a control-plane action, or raise with a specific complaint."""

        def need(field: str) -> Any:
            value = getattr(self, field)
            if value is None:
                raise InvalidProposedAction(f"{self.kind} requires '{field}'")
            return value

        match self.kind:
            case "rollback_deploy":
                # to_version is left unset so the control plane resolves the service's recorded
                # previous version, rather than trusting a version string the model invented.
                return RollbackDeploy(service=str(need("service")), to_version=None)
            case "toggle_feature_flag":
                return ToggleFeatureFlag(
                    flag=str(need("flag")), enabled=bool(need("enabled")), region=self.region
                )
            case "set_config_value":
                return SetConfigValue(key=str(need("key")), value=need("value"))
            case "scale_replicas":
                return ScaleReplicas(service=str(need("service")), replicas=int(need("replicas")))
            case "restart_service":
                return RestartService(service=str(need("service")))
            case "no_action":
                return NoAction(reason=self.reason or self.rationale)

    def describe(self) -> str:
        return self.to_action().describe()


class IncidentProposal(BaseModel):
    """The agent's complete answer for one incident."""

    severity: Severity = Field(
        description="Classified per the severity policy: customer impact, not metric magnitude."
    )
    hypothesis: str = Field(description="The most likely cause, stated in one or two sentences.")
    confidence: Confidence
    summary: str = Field(description="A short status summary suitable for posting to Slack.")

    evidence_cited: list[str] = Field(
        default_factory=list,
        description=(
            "Identifiers for the evidence supporting the hypothesis — tool names for telemetry "
            "(metrics, logs, changes, service_state) and chunk ids for retrieved documents. "
            "Every substantive claim should be traceable to one of these."
        ),
    )
    similar_incidents: list[str] = Field(
        default_factory=list, description="Keys of prior incidents that informed the diagnosis."
    )
    ruled_out: list[str] = Field(
        default_factory=list,
        description="Hypotheses considered and rejected, with the evidence that rejected them.",
    )

    actions: list[ProposedAction] = Field(
        default_factory=list,
        description=(
            "Remediation in the order it should be applied. Use a single no_action entry when "
            "the correct answer is to change nothing."
        ),
    )
    verification_plan: str = Field(
        description="Which metrics should move, and to what, if the diagnosis is right."
    )
    requires_followup: bool = Field(
        description="True when the proposed action mitigates without fixing the underlying cause."
    )
    followup_note: str | None = Field(
        default=None, description="What still needs doing when requires_followup is true."
    )
    next_update_minutes: int = Field(default=15, ge=1, le=240)

    def validated_actions(self) -> list[Action]:
        return [action.to_action() for action in self.actions]

    def is_no_action(self) -> bool:
        return len(self.actions) == 1 and self.actions[0].kind == "no_action"


class InvestigationResult(BaseModel):
    """What the investigation phase gathered, before any judgement is applied."""

    transcript: list[str] = Field(
        default_factory=list, description="Human-readable log of tool calls, for the audit trail."
    )
    tool_calls: int = 0
    evidence: dict[str, object] = Field(default_factory=dict)
    cited_chunks: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
