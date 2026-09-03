"""The agent's tool surface.

Two things are under test: that the tools return accurate, self-describing evidence, and that the
surface contains no way to change anything.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from incident_copilot.agent import tools as agent_tools
from incident_copilot.agent.tools import ToolContext, build_tools, tool_names
from incident_copilot.controlplane.actions import SetConfigValue, apply_action
from incident_copilot.controlplane.scenarios import get_registry
from incident_copilot.retrieval.index import reindex

REGISTRY = get_registry()


@pytest.fixture
def indexed(db: Session) -> Iterator[Session]:
    reindex(db)
    db.commit()
    yield db


def context(session: Session, scenario_key: str = "payments_gateway_timeout") -> ToolContext:
    scenario = REGISTRY.get(scenario_key)
    return ToolContext(session=session, scenario=scenario, state=scenario.initial_state())


def call(tools: list[object], name: str, **kwargs: object) -> object:
    tool = next(t for t in tools if getattr(t, "name", None) == name)
    return json.loads(tool(**kwargs))  # type: ignore[operator]


# -- the read-only boundary -------------------------------------------------


def test_the_tool_surface_contains_no_mutation(indexed: Session) -> None:
    """The central safety property of the whole design.

    Investigation cannot change the system because there is no tool that can. This is structural
    rather than instructional — a prompt can be talked out of a rule, an absent function cannot
    be called.
    """
    names = tool_names(build_tools(context(indexed)))
    assert names == [
        "get_metrics",
        "get_logs",
        "get_recent_changes",
        "get_service_state",
        "search_runbooks",
        "find_similar_incidents",
    ]
    forbidden = {"rollback", "toggle", "set_config", "scale", "restart", "apply", "execute"}
    assert not any(word in n for n in names for word in forbidden)


def test_tools_module_does_not_import_mutations() -> None:
    """Guards against a future edit importing apply_action into the tool module."""
    agent_tools.assert_read_only()


def test_investigating_does_not_change_state(indexed: Session) -> None:
    ctx = context(indexed)
    before = ctx.state.model_copy(deep=True)
    tools = build_tools(ctx)
    for name in ("get_metrics", "get_logs", "get_recent_changes", "get_service_state"):
        call(tools, name)
    call(tools, "search_runbooks", query="gateway")
    call(tools, "find_similar_incidents", query="payments timeout")
    assert ctx.state == before


# -- telemetry tools --------------------------------------------------------


def test_get_metrics_carries_baseline_and_slo(indexed: Session) -> None:
    """Numbers alone are unjudgeable; the model needs to know what normal and breaching are."""
    payload = call(build_tools(context(indexed)), "get_metrics")
    assert isinstance(payload, dict)
    assert payload["current"]["error_rate"] == pytest.approx(0.124)
    assert payload["baseline"]["error_rate"] == pytest.approx(0.002)
    assert payload["slo"]["max_error_rate"] == pytest.approx(0.01)


def test_get_metrics_reflects_remediated_state(indexed: Session) -> None:
    """Tools read live state, so a second investigation after a fix sees the fix."""
    ctx = context(indexed)
    ctx.state = apply_action(ctx.state, SetConfigValue(key="gateway_timeout_ms", value=2000)).state
    payload = call(build_tools(ctx), "get_metrics")
    assert payload["current"]["error_rate"] == pytest.approx(0.002)  # type: ignore[index]


def test_get_logs_substitutes_live_values(indexed: Session) -> None:
    payload = call(build_tools(context(indexed)), "get_logs")
    joined = " ".join(payload["lines"])  # type: ignore[index]
    assert "1000ms" in joined
    assert "CircuitBreakerOpenException" in joined


def test_get_recent_changes_is_newest_first_and_covers_all_kinds(indexed: Session) -> None:
    payload = call(build_tools(context(indexed)), "get_recent_changes")
    assert isinstance(payload, list)
    assert [c["at"] for c in payload] == sorted((c["at"] for c in payload), reverse=True)
    # Config and flag changes must be visible, not just deploys — that is the whole diagnosis.
    assert {c["kind"] for c in payload} >= {"deploy", "feature_flag", "config"}


def test_get_service_state_exposes_flags_and_config(indexed: Session) -> None:
    payload = call(build_tools(context(indexed)), "get_service_state")
    assert payload["config"]["gateway_timeout_ms"] == 1000  # type: ignore[index]
    assert payload["feature_flags"]["enable_new_gateway"]["us-east"] is True  # type: ignore[index]
    assert payload["services"]["payments-api"]["previous_version"] == "v2.4.0"  # type: ignore[index]


# -- retrieval tools --------------------------------------------------------


def test_search_runbooks_returns_citable_chunks(indexed: Session) -> None:
    ctx = context(indexed)
    payload = call(build_tools(ctx), "search_runbooks", query="gateway timeout circuit breaker")
    assert isinstance(payload, list) and payload
    assert all(hit["chunk_id"].startswith("kb:") for hit in payload)
    assert any("payments" in hit["title"].lower() for hit in payload)
    assert ctx.cited_chunks  # recorded for the audit trail


def test_find_similar_incidents_returns_resolutions(indexed: Session) -> None:
    """A precedent is only useful if it says what actually fixed it."""
    payload = call(
        build_tools(context(indexed)),
        "find_similar_incidents",
        query="payments-api upstream gateway timeout error rate spike",
    )
    assert isinstance(payload, list) and payload
    assert any("INC-2025-0412" in str(hit["incident"]) for hit in payload)
    assert all("Resolution:" in hit["detail"] for hit in payload)


def test_empty_search_returns_guidance_not_an_error(indexed: Session) -> None:
    """A dead end must not look like a tool failure, or the model retries pointlessly."""
    tool = next(t for t in build_tools(context(indexed)) if t.name == "search_runbooks")
    assert "No runbook sections matched" in tool(query="zzzzqqq nonexistent")


def test_natural_language_queries_retrieve(indexed: Session) -> None:
    """Regression guard: an AND-joined tsquery silently returned nothing for prose queries."""
    tools = build_tools(context(indexed))
    for query in (
        "why are tokens expiring immediately after login",
        "latency is high but the error rate is flat",
        "memory keeps growing since the last restart",
    ):
        assert call(tools, "search_runbooks", query=query), f"no hits for {query!r}"


# -- transcript -------------------------------------------------------------


def test_transcript_records_every_call(indexed: Session) -> None:
    """The audit trail is what lets a citation be checked rather than trusted."""
    ctx = context(indexed)
    tools = build_tools(ctx)
    call(tools, "get_metrics")
    call(tools, "search_runbooks", query="gateway")

    assert ctx.calls == 2
    assert ctx.transcript == ["get_metrics()", "search_runbooks(gateway)"]
    assert "get_metrics" in ctx.evidence


@pytest.mark.parametrize("scenario_key", [s.key for s in REGISTRY.all()])
def test_tools_work_for_every_scenario(indexed: Session, scenario_key: str) -> None:
    ctx = context(indexed, scenario_key)
    tools = build_tools(ctx)
    metrics = call(tools, "get_metrics")
    assert metrics["current"]["request_rate_rps"] > 0  # type: ignore[index]
    assert call(tools, "get_service_state")
    assert isinstance(call(tools, "get_logs")["lines"], list)  # type: ignore[index]
