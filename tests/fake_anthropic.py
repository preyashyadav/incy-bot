"""A scripted stand-in for the Anthropic client.

Tests must not hit the network: a suite whose result depends on a live model is neither fast nor
deterministic, and CI cannot depend on an API key. But a double that returns a canned proposal
without touching the tools would test nothing — it would pass even if every tool were broken.

So this double *really runs the tool loop*. `tool_runner` executes the scripted tool calls
against the actual tool functions, which query the actual database and simulator. What is faked
is only the model's choice of which tools to call and what to conclude. Everything downstream of
that choice is real.

Live-API coverage lives in `tests/test_agent_live.py` behind the `live` marker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from incident_copilot.agent.schemas import IncidentProposal


@dataclass
class Usage:
    input_tokens: int = 1200
    output_tokens: int = 300


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeMessage:
    content: list[TextBlock]
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "end_turn"


class ScriptedToolRunner:
    """Iterates once, having executed every scripted tool call against the real tools."""

    def __init__(self, tools: list[Any], script: list[tuple[str, dict[str, Any]]], findings: str):
        self._tools = {getattr(t, "name", getattr(t, "__name__", "?")): t for t in tools}
        self._script = script
        self._findings = findings
        self.executed: list[str] = []
        self.tool_outputs: list[str] = []

    def __iter__(self) -> Any:
        for name, kwargs in self._script:
            tool = self._tools.get(name)
            if tool is None:
                raise AssertionError(
                    f"script calls unknown tool {name!r}; available: {sorted(self._tools)}"
                )
            # BetaFunctionTool is directly callable and validates its arguments, so this
            # exercises the same input handling the real runner performs.
            output = tool(**kwargs)
            self.executed.append(name)
            self.tool_outputs.append(str(output))
        yield FakeMessage(content=[TextBlock(text=self._findings)])


class FakeBetaMessages:
    def __init__(self, parent: FakeAnthropic) -> None:
        self._parent = parent

    def tool_runner(self, **kwargs: Any) -> ScriptedToolRunner:
        self._parent.investigate_calls.append(kwargs)
        runner = ScriptedToolRunner(
            tools=kwargs["tools"], script=self._parent.script, findings=self._parent.findings
        )
        self._parent.last_runner = runner
        return runner


class FakeBeta:
    def __init__(self, parent: FakeAnthropic) -> None:
        self.messages = FakeBetaMessages(parent)


@dataclass
class ParsedResponse:
    parsed_output: IncidentProposal | None
    usage: Usage = field(default_factory=Usage)


class FakeMessages:
    def __init__(self, parent: FakeAnthropic) -> None:
        self._parent = parent

    def parse(self, **kwargs: Any) -> ParsedResponse:
        self._parent.propose_calls.append(kwargs)
        return ParsedResponse(parsed_output=self._parent.proposal)


class FakeAnthropic:
    """Drop-in for `AnthropicClient`.

    `script` is the sequence of (tool_name, kwargs) the "model" chooses to call; `proposal` is
    what it concludes. Set `proposal=None` to simulate an unparsable response.
    """

    def __init__(
        self,
        *,
        script: list[tuple[str, dict[str, Any]]] | None = None,
        proposal: IncidentProposal | None = None,
        findings: str = "Investigated; see tool output.",
    ) -> None:
        self.script = script if script is not None else DEFAULT_SCRIPT
        self.proposal = proposal
        self.findings = findings
        self.investigate_calls: list[dict[str, Any]] = []
        self.propose_calls: list[dict[str, Any]] = []
        self.last_runner: ScriptedToolRunner | None = None
        self.beta = FakeBeta(self)
        self.messages = FakeMessages(self)

    # -- assertions used by tests -----------------------------------------

    def tools_offered(self) -> list[str]:
        call = self.investigate_calls[-1]
        return [getattr(t, "name", getattr(t, "__name__", "?")) for t in call["tools"]]

    def tool_output_for(self, name: str) -> Any:
        assert self.last_runner is not None
        index = self.last_runner.executed.index(name)
        return json.loads(self.last_runner.tool_outputs[index])


DEFAULT_SCRIPT: list[tuple[str, dict[str, Any]]] = [
    ("get_metrics", {}),
    ("get_logs", {}),
    ("get_recent_changes", {}),
    ("get_service_state", {}),
    ("search_runbooks", {"query": "gateway timeout circuit breaker"}),
    ("find_similar_incidents", {"query": "payments-api error rate spike upstream timeout"}),
]


def make_proposal(**overrides: Any) -> IncidentProposal:
    """A plausible proposal for the payments scenario, for tests that need a valid one."""
    payload: dict[str, Any] = {
        "severity": "SEV1",
        "hypothesis": "gateway_timeout_ms was lowered to 1000 while enable_new_gateway is on in "
        "us-east; the new client cannot complete inside that budget.",
        "confidence": "high",
        "summary": "Payments failing in us-east from a gateway timeout set below the new "
        "client's floor.",
        "evidence_cited": [
            "get_metrics",
            "get_recent_changes",
            "kb:runbook-payments-gateway:mitigation",
        ],
        "similar_incidents": ["INC-2025-0412"],
        "ruled_out": ["Deploy v2.4.1 — ships both clients, selected by flag"],
        "actions": [
            {
                "kind": "set_config_value",
                "key": "gateway_timeout_ms",
                "value": 2000,
                "rationale": "Reverses the change that introduced the fault.",
                "risk": "low",
                "reversible": True,
            }
        ],
        "verification_plan": "error_rate returns below 1% and upstream_timeout_rate to zero.",
        "requires_followup": False,
        "followup_note": None,
        "next_update_minutes": 15,
    }
    payload.update(overrides)
    return IncidentProposal.model_validate(payload)
