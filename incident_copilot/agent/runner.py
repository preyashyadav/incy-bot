"""The two-phase agent: investigate, then propose.

**Phase A — investigate.** A tool-use loop over the read-only tool surface. The model decides
what to look at and in what order.

**Phase B — propose.** A separate structured-output call over the gathered evidence, returning a
schema-valid `IncidentProposal`.

The phases are split deliberately. Running a tool loop under an output constraint makes the model
fight two objectives at once, and it conflates two independently testable things: whether the
agent gathered the right evidence, and whether it drew the right conclusion from it. Split, a bad
proposal from good evidence is a prompt problem and a good proposal from thin evidence is a tool
problem — and the transcript says which.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import anthropic
from sqlalchemy.orm import Session

from incident_copilot.agent import prompts
from incident_copilot.agent.schemas import IncidentProposal, InvestigationResult
from incident_copilot.agent.tools import ToolContext, build_tools
from incident_copilot.config import Settings, get_settings
from incident_copilot.controlplane.scenarios import Scenario
from incident_copilot.controlplane.state import ControlPlaneState

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """The agent could not produce a usable proposal."""


class AnthropicClient(Protocol):
    """The slice of the SDK this module uses.

    Narrow on purpose: the test double implements six attributes rather than the whole client,
    and the type checker enforces that this stays true.
    """

    @property
    def beta(self) -> Any: ...
    @property
    def messages(self) -> Any: ...


def build_client(settings: Settings | None = None) -> AnthropicClient:
    """Construct an Anthropic client.

    No API key is passed explicitly when the setting is unset: the SDK also resolves an
    `ant auth login` profile, and forcing `api_key=None` would defeat that.
    """
    settings = settings or get_settings()
    if settings.anthropic_api_key is not None:
        return anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    return anthropic.Anthropic()


def investigate(
    client: AnthropicClient,
    session: Session,
    scenario: Scenario,
    state: ControlPlaneState,
    *,
    settings: Settings | None = None,
) -> tuple[InvestigationResult, str]:
    """Run the evidence-gathering loop. Returns the result and the model's written findings."""
    settings = settings or get_settings()
    ctx = ToolContext(session=session, scenario=scenario, state=state)
    tools = build_tools(ctx)

    alert = scenario.alert.model_dump(mode="json")
    runner = client.beta.messages.tool_runner(
        model=settings.anthropic_model,
        max_tokens=8000,
        system=[
            {
                "type": "text",
                "text": prompts.INVESTIGATE_SYSTEM,
                # The system prompt and tool definitions are identical for every incident, so
                # this prefix is cacheable across the whole workload.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        thinking={"type": "adaptive"},
        output_config={"effort": settings.agent_investigate_effort},
        tools=tools,
        messages=[
            {
                "role": "user",
                "content": prompts.investigation_task(alert, scenario.service, state.region),
            }
        ],
    )

    findings = ""
    input_tokens = output_tokens = 0
    for index, message in enumerate(runner):
        usage = getattr(message, "usage", None)
        if usage is not None:
            input_tokens += getattr(usage, "input_tokens", 0) or 0
            output_tokens += getattr(usage, "output_tokens", 0) or 0

        text = _text_of(message)
        if text:
            findings = text

        if index + 1 >= settings.agent_max_tool_iterations:
            logger.warning(
                "investigation hit the %s-iteration ceiling for %s",
                settings.agent_max_tool_iterations,
                scenario.key,
            )
            break

    if not ctx.calls:
        # A proposal built on no evidence is worse than no proposal: it looks authoritative and
        # is pure prior. Fail instead of passing it downstream.
        raise AgentError("the agent proposed without calling a single tool")

    result = InvestigationResult(
        transcript=ctx.transcript,
        tool_calls=ctx.calls,
        evidence=dict(ctx.evidence),
        cited_chunks=list(dict.fromkeys(ctx.cited_chunks)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return result, findings or "(the model recorded no written findings)"


def propose(
    client: AnthropicClient,
    result: InvestigationResult,
    findings: str,
    *,
    settings: Settings | None = None,
) -> IncidentProposal:
    """Turn gathered evidence into a schema-valid proposal."""
    settings = settings or get_settings()
    record = _render_record(result, findings)

    response = client.messages.parse(
        model=settings.anthropic_model,
        max_tokens=8000,
        system=[
            {
                "type": "text",
                "text": prompts.PROPOSE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompts.proposal_task(record)}],
        output_format=IncidentProposal,
    )

    proposal: IncidentProposal | None = response.parsed_output
    if proposal is None:
        raise AgentError("the model returned no parsable proposal")
    if not proposal.actions:
        # An empty list is ambiguous between "nothing to do" and "I gave up". `no_action` is the
        # explicit, recordable way to say the former.
        raise AgentError("proposal contained no actions; expected at least a no_action entry")
    return proposal


def run_agent(
    session: Session,
    scenario: Scenario,
    state: ControlPlaneState,
    *,
    client: AnthropicClient | None = None,
    settings: Settings | None = None,
) -> tuple[IncidentProposal, InvestigationResult]:
    """Investigate, then propose."""
    settings = settings or get_settings()
    client = client or build_client(settings)

    result, findings = investigate(client, session, scenario, state, settings=settings)
    logger.info(
        "investigation of %s used %s tool call(s): %s",
        scenario.key,
        result.tool_calls,
        ", ".join(result.transcript),
    )
    proposal = propose(client, result, findings, settings=settings)
    return proposal, result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_of(message: Any) -> str:
    """Concatenate the text blocks of a message, ignoring thinking and tool_use blocks."""
    content = getattr(message, "content", None)
    if content is None:
        return ""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(p for p in parts if p).strip()


def _render_record(result: InvestigationResult, findings: str) -> str:
    """Present the investigation to the proposal call.

    The raw tool output is replayed alongside the model's own summary rather than the summary
    alone: a summary is lossy, and the proposal step needs the actual numbers to classify
    severity against an SLO.
    """
    import json

    sections = [f"## Your findings\n\n{findings}", "\n## Tools you called\n"]
    sections.extend(f"- {entry}" for entry in result.transcript)
    sections.append("\n## Raw evidence\n")
    for tool, payload in result.evidence.items():
        sections.append(f"### {tool}\n```json\n{json.dumps(payload, indent=2)}\n```")
    return "\n".join(sections)
