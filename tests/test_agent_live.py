"""Live-model tests. Excluded from CI; run with `pytest -m live`.

Everything else in the suite fakes the model's *choices* while running the real tools. These
tests check the part that cannot be faked: whether Claude, given this tool surface and these
prompts, actually reaches the right diagnosis.

They cost money and are non-deterministic, so they assert on outcomes that should hold for any
competent investigation — the proposed action resolves the scenario, no-action scenarios get no
action — rather than on exact wording.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from incident_copilot.agent.runner import build_client, run_agent
from incident_copilot.config import get_settings
from incident_copilot.controlplane.actions import NoAction, apply_action
from incident_copilot.controlplane.scenarios import get_registry
from incident_copilot.controlplane.simulator import assess_health
from incident_copilot.retrieval.index import reindex

pytestmark = pytest.mark.live

REGISTRY = get_registry()


@pytest.fixture(scope="module")
def client() -> object:
    settings = get_settings()
    try:
        return build_client(settings)
    except Exception as exc:  # no key and no `ant auth login` profile
        pytest.skip(f"no Anthropic credentials available: {exc}")


@pytest.fixture
def indexed(db: Session) -> Iterator[Session]:
    reindex(db)
    db.commit()
    yield db


@pytest.mark.parametrize("scenario_key", [s.key for s in REGISTRY.all()])
def test_agent_resolves_each_scenario(indexed: Session, client: object, scenario_key: str) -> None:
    """The real test of the whole system: does the proposal actually fix the incident?

    Asserted against the simulator rather than against the ground-truth action, so an
    alternative correct remediation passes. What matters is that the system recovers.
    """
    scenario = REGISTRY.get(scenario_key)
    state = scenario.initial_state()
    proposal, result = run_agent(indexed, scenario, state, client=client)  # type: ignore[arg-type]

    assert result.tool_calls >= 3, "investigated with implausibly little evidence"
    assert proposal.evidence_cited, "proposal cited no evidence"

    healthy_before = assess_health(scenario, state).healthy
    for action in proposal.validated_actions():
        if isinstance(action, NoAction):
            continue
        state = apply_action(state, action).state

    assessment = assess_health(scenario, state)
    assert assessment.healthy, (
        f"{scenario_key} not resolved by {[a.kind for a in proposal.actions]}: "
        f"{assessment.describe()} | hypothesis: {proposal.hypothesis}"
    )
    if not healthy_before:
        assert not proposal.is_no_action(), "proposed no action on a genuinely broken system"


def test_agent_declines_to_act_on_a_healthy_system(indexed: Session, client: object) -> None:
    """The hardest judgement in the catalogue, and the one most worth verifying live."""
    scenario = REGISTRY.get("noisy_neighbor_traffic_surge")
    proposal, _ = run_agent(indexed, scenario, scenario.initial_state(), client=client)  # type: ignore[arg-type]

    assert proposal.is_no_action(), (
        f"proposed {[a.kind for a in proposal.actions]} on a system inside SLO; "
        f"hypothesis: {proposal.hypothesis}"
    )
    assert proposal.severity == "SEV3"


def test_agent_classifies_degradation_below_outage(indexed: Session, client: object) -> None:
    """Severity by customer impact, not metric magnitude — p95 up 10x with errors flat."""
    scenario = REGISTRY.get("latency_regression_n_plus_one")
    proposal, _ = run_agent(indexed, scenario, scenario.initial_state(), client=client)  # type: ignore[arg-type]
    assert proposal.severity in {"SEV2", "SEV3"}


def test_agent_flags_a_mitigation_as_needing_followup(indexed: Session, client: object) -> None:
    """A restart against a leak resolves the metrics and leaves the defect in place."""
    scenario = REGISTRY.get("checkout_memory_leak")
    proposal, _ = run_agent(indexed, scenario, scenario.initial_state(), client=client)  # type: ignore[arg-type]
    assert proposal.requires_followup, "restart-as-mitigation was not flagged for follow-up"
    assert proposal.followup_note
