"""The two-phase agent, and the schema contract between the model and the system."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from incident_copilot.agent.runner import AgentError, investigate, propose, run_agent
from incident_copilot.agent.schemas import (
    IncidentProposal,
    InvalidProposedAction,
    ProposedAction,
)
from incident_copilot.config import Settings
from incident_copilot.controlplane.actions import (
    NoAction,
    RestartService,
    RollbackDeploy,
    ScaleReplicas,
    SetConfigValue,
    ToggleFeatureFlag,
    apply_action,
)
from incident_copilot.controlplane.scenarios import get_registry
from incident_copilot.controlplane.simulator import assess_health
from incident_copilot.retrieval.index import reindex
from tests.fake_anthropic import DEFAULT_SCRIPT, FakeAnthropic, make_proposal

REGISTRY = get_registry()
SCENARIO = REGISTRY.get("payments_gateway_timeout")


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, agent_max_tool_iterations=12)  # type: ignore[call-arg]


@pytest.fixture
def indexed(db: Session) -> Iterator[Session]:
    reindex(db)
    db.commit()
    yield db


# -- ProposedAction conversion ----------------------------------------------


def test_each_action_kind_converts() -> None:
    cases = [
        (dict(kind="rollback_deploy", service="api"), RollbackDeploy),
        (dict(kind="toggle_feature_flag", flag="f", enabled=False), ToggleFeatureFlag),
        (dict(kind="set_config_value", key="k", value=2000), SetConfigValue),
        (dict(kind="scale_replicas", service="api", replicas=8), ScaleReplicas),
        (dict(kind="restart_service", service="api"), RestartService),
        (dict(kind="no_action", reason="within SLO"), NoAction),
    ]
    for payload, expected in cases:
        action = ProposedAction(rationale="r", risk="low", reversible=True, **payload).to_action()  # type: ignore[arg-type]
        assert isinstance(action, expected)


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        (dict(kind="rollback_deploy"), "service"),
        (dict(kind="toggle_feature_flag", enabled=True), "flag"),
        (dict(kind="toggle_feature_flag", flag="f"), "enabled"),
        (dict(kind="set_config_value", key="k"), "value"),
        (dict(kind="scale_replicas", service="api"), "replicas"),
        (dict(kind="restart_service"), "service"),
    ],
)
def test_missing_field_names_the_field(payload: dict[str, object], missing: str) -> None:
    """The error is reportable into the Slack thread, so it has to say what was wrong."""
    action = ProposedAction(rationale="r", risk="low", reversible=True, **payload)  # type: ignore[arg-type]
    with pytest.raises(InvalidProposedAction, match=missing):
        action.to_action()


def test_no_action_falls_back_to_rationale() -> None:
    action = ProposedAction(
        kind="no_action", rationale="Everything is inside SLO.", risk="low", reversible=True
    )
    assert "inside SLO" in action.to_action().describe()


def test_proposal_detects_a_no_action_answer() -> None:
    assert make_proposal(
        actions=[{"kind": "no_action", "rationale": "fine", "risk": "low", "reversible": True}]
    ).is_no_action()
    assert not make_proposal().is_no_action()


# -- investigation phase ----------------------------------------------------


def test_investigation_runs_the_real_tools(indexed: Session, settings: Settings) -> None:
    client = FakeAnthropic()
    result, findings = investigate(
        client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings
    )

    assert result.tool_calls == len(DEFAULT_SCRIPT)
    assert "get_metrics" in result.evidence
    # Evidence is the genuine simulator output, not a canned fixture.
    assert result.evidence["get_metrics"]["current"]["error_rate"] == pytest.approx(0.124)  # type: ignore[index,call-overload]
    assert result.cited_chunks
    assert findings


def test_investigation_offers_only_read_only_tools(indexed: Session, settings: Settings) -> None:
    client = FakeAnthropic()
    investigate(client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings)
    assert client.tools_offered() == [
        "get_metrics",
        "get_logs",
        "get_recent_changes",
        "get_service_state",
        "search_runbooks",
        "find_similar_incidents",
    ]


def test_investigation_requests_a_cacheable_prefix(indexed: Session, settings: Settings) -> None:
    """The system prompt is identical across incidents, so it should be cached."""
    client = FakeAnthropic()
    investigate(client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings)
    system = client.investigate_calls[-1]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_system_prompt_carries_no_per_incident_content(
    indexed: Session, settings: Settings
) -> None:
    """Interpolating an incident id or timestamp here would invalidate the cache every request."""
    client = FakeAnthropic()
    state = SCENARIO.initial_state()
    investigate(client, indexed, SCENARIO, state, settings=settings)
    system_text = client.investigate_calls[-1]["system"][0]["text"]

    for volatile in (SCENARIO.key, SCENARIO.service, state.region, str(state.clock.year)):
        assert volatile not in system_text
    # …and the volatile detail is present in the user turn instead.
    user = client.investigate_calls[-1]["messages"][0]["content"]
    assert SCENARIO.service in user and state.region in user


def test_investigation_uses_adaptive_thinking_and_configured_effort(
    indexed: Session,
) -> None:
    client = FakeAnthropic()
    settings = Settings(_env_file=None, agent_investigate_effort="xhigh")  # type: ignore[call-arg]
    investigate(client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings)
    call = client.investigate_calls[-1]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == "xhigh"
    assert call["model"] == "claude-opus-5"


def test_investigation_without_any_tool_call_is_an_error(
    indexed: Session, settings: Settings
) -> None:
    """A proposal built on no evidence is pure prior dressed as a diagnosis."""
    client = FakeAnthropic(script=[])
    with pytest.raises(AgentError, match="without calling a single tool"):
        investigate(client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings)


def test_unknown_tool_in_script_is_caught(indexed: Session, settings: Settings) -> None:
    client = FakeAnthropic(script=[("delete_everything", {})])
    with pytest.raises(AssertionError, match="unknown tool"):
        investigate(client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings)


# -- proposal phase ---------------------------------------------------------


def test_proposal_phase_returns_the_structured_object(indexed: Session, settings: Settings) -> None:
    client = FakeAnthropic(proposal=make_proposal())
    result, findings = investigate(
        client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings
    )
    proposal = propose(client, result, findings, settings=settings)

    assert isinstance(proposal, IncidentProposal)
    assert proposal.severity == "SEV1"
    assert proposal.actions[0].kind == "set_config_value"


def test_proposal_phase_receives_raw_evidence_not_just_a_summary(
    indexed: Session, settings: Settings
) -> None:
    """Severity must be judged against actual numbers; a prose summary loses them."""
    client = FakeAnthropic(proposal=make_proposal())
    result, findings = investigate(
        client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings
    )
    propose(client, result, findings, settings=settings)

    sent = client.propose_calls[-1]["messages"][0]["content"]
    assert "0.124" in sent  # the real error rate reached the proposal call
    assert "max_error_rate" in sent  # …and the SLO it breaches
    assert "get_metrics" in sent


def test_unparsable_response_raises(indexed: Session, settings: Settings) -> None:
    client = FakeAnthropic(proposal=None)
    result, findings = investigate(
        client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings
    )
    with pytest.raises(AgentError, match="no parsable proposal"):
        propose(client, result, findings, settings=settings)


def test_empty_action_list_is_rejected(indexed: Session, settings: Settings) -> None:
    """Ambiguous between 'nothing to do' and 'I gave up'; no_action says the former explicitly."""
    client = FakeAnthropic(proposal=make_proposal(actions=[]))
    result, findings = investigate(
        client, indexed, SCENARIO, SCENARIO.initial_state(), settings=settings
    )
    with pytest.raises(AgentError, match="no actions"):
        propose(client, result, findings, settings=settings)


# -- end to end -------------------------------------------------------------


def test_run_agent_produces_an_executable_proposal(indexed: Session, settings: Settings) -> None:
    """The join between the model's answer and the control plane."""
    client = FakeAnthropic(proposal=make_proposal())
    proposal, result = run_agent(
        indexed, SCENARIO, SCENARIO.initial_state(), client=client, settings=settings
    )

    actions = proposal.validated_actions()
    assert len(actions) == 1

    state = SCENARIO.initial_state()
    assert not assess_health(SCENARIO, state).healthy
    outcome = apply_action(state, actions[0])
    assert assess_health(SCENARIO, outcome.state).healthy
    assert result.tool_calls == len(DEFAULT_SCRIPT)


def test_a_wrong_proposal_leaves_the_incident_broken(indexed: Session, settings: Settings) -> None:
    """The harness must not flatter the model — a bad answer has to fail verification."""
    wrong = make_proposal(
        actions=[
            {
                "kind": "scale_replicas",
                "service": "payments-api",
                "replicas": 12,
                "rationale": "add capacity",
                "risk": "low",
                "reversible": True,
            }
        ]
    )
    client = FakeAnthropic(proposal=wrong)
    proposal, _ = run_agent(
        indexed, SCENARIO, SCENARIO.initial_state(), client=client, settings=settings
    )

    outcome = apply_action(SCENARIO.initial_state(), proposal.validated_actions()[0])
    assert not assess_health(SCENARIO, outcome.state).healthy


@pytest.mark.parametrize("scenario_key", [s.key for s in REGISTRY.all()])
def test_agent_runs_against_every_scenario(
    indexed: Session, settings: Settings, scenario_key: str
) -> None:
    """The tool surface must work for every incident type, not just the flagship one."""
    scenario = REGISTRY.get(scenario_key)
    script = [
        ("get_metrics", {}),
        ("get_logs", {}),
        ("get_recent_changes", {}),
        ("get_service_state", {}),
        ("search_runbooks", {"query": f"{scenario.service} {scenario.alert.signal}"}),
        ("find_similar_incidents", {"query": f"{scenario.service} {scenario.alert.signal}"}),
    ]
    proposed = ProposedAction.model_validate(
        {
            **scenario.ground_truth.action.model_dump(),
            "rationale": "r",
            "risk": "low",
            "reversible": True,
        }
    )
    client = FakeAnthropic(
        script=script,
        proposal=make_proposal(
            severity=scenario.expected_severity, actions=[proposed.model_dump()]
        ),
    )
    proposal, result = run_agent(
        indexed, scenario, scenario.initial_state(), client=client, settings=settings
    )

    assert result.tool_calls == 6
    # Ground truth resolves the scenario, so a correctly-reasoned proposal must too.
    outcome = apply_action(scenario.initial_state(), proposal.validated_actions()[0])
    assert assess_health(scenario, outcome.state).healthy
