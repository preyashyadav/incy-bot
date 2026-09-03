"""The dashboard.

Rendering tests, but not shallow ones: the point of this surface is provenance, so what is
asserted is that a citation actually resolves to the document it names and that the document
knows which incidents used it. A dashboard that renders but whose links go nowhere would pass a
smoke test and fail the only job it has.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from incident_copilot.api.main import app
from incident_copilot.db import repositories as repo
from incident_copilot.db.models import EventType, ExecutionStatus, Incident, Proposal
from incident_copilot.retrieval.index import reindex
from incident_copilot.slack import service

CHANNEL = "C0INCIDENT"
CHUNK = "kb:runbook-payments-gateway:mitigation"


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    reindex(db)
    db.commit()
    yield TestClient(app)


@pytest.fixture
def investigated(db: Session, client: TestClient) -> tuple[Incident, Proposal]:
    """An incident carrying the full evidence trail a real investigation leaves behind."""
    triaged = service.triage_scenario(
        db, "payments_gateway_timeout", channel_id=CHANNEL, actor="U0PREYASH"
    )
    incident = triaged.incident
    repo.append_event(db, incident, EventType.INVESTIGATION_STARTED)
    repo.append_event(
        db,
        incident,
        EventType.EVIDENCE_GATHERED,
        {
            "tool_calls": 6,
            "transcript": ["get_metrics()", "search_runbooks(gateway timeout)"],
            "cited_chunks": [CHUNK, "history:INC-2025-0412"],
            "input_tokens": 14213,
            "output_tokens": 902,
        },
    )
    proposal = repo.create_proposal(
        db,
        incident,
        severity="SEV1",
        hypothesis="gateway_timeout_ms was halved below the new client's floor.",
        confidence="high",
        actions=[
            {
                "kind": "set_config_value",
                "key": "gateway_timeout_ms",
                "value": 2000,
                "rationale": "Reverses the change that broke it.",
                "risk": "low",
                "reversible": True,
            }
        ],
        evidence_cited=["get_metrics", CHUNK],
        similar_incidents=["INC-2025-0412"],
        verification_plan="error_rate returns below 1%.",
        raw={"ruled_out": ["Deploy v2.4.1 — ships both clients"]},
    )
    db.commit()
    return incident, proposal


# -- incident list ----------------------------------------------------------


def test_empty_dashboard_explains_itself(client: TestClient) -> None:
    body = client.get("/dashboard").text
    assert "Nothing yet" in body
    assert "/incident" in body


def test_list_shows_the_incident_and_its_proposed_fix(
    client: TestClient, investigated: tuple[Incident, Proposal]
) -> None:
    incident, _ = investigated
    body = client.get("/dashboard").text
    assert incident.key in body
    assert "set_config_value" in body
    assert "SEV1" in body


def test_list_skips_the_placeholder_proposal(client: TestClient, db: Session) -> None:
    """Triage creates an empty proposal to hang a token off; it is not a proposed fix."""
    service.triage_scenario(db, "checkout_memory_leak", channel_id=CHANNEL, actor="U1")
    db.commit()
    body = client.get("/dashboard").text
    assert "checkout" in body
    assert "action(s) proposed" not in body


# -- incident detail --------------------------------------------------------


def test_detail_shows_diagnosis_and_actions(
    client: TestClient, investigated: tuple[Incident, Proposal]
) -> None:
    incident, _ = investigated
    body = client.get(f"/dashboard/incidents/{incident.key}").text
    assert "gateway_timeout_ms was halved" in body
    assert "Set gateway_timeout_ms to 2000" in body
    assert "low risk" in body
    assert "reversible" in body
    assert "Reverses the change that broke it." in body
    assert "Deploy v2.4.1" in body  # ruled out


def test_detail_shows_where_the_answer_came_from(
    client: TestClient, investigated: tuple[Incident, Proposal]
) -> None:
    incident, _ = investigated
    body = client.get(f"/dashboard/incidents/{incident.key}").text
    assert "get_metrics()" in body  # tool transcript
    assert "14,213" in body  # token accounting
    assert f'href="/dashboard/kb/{CHUNK}"' in body  # citation links somewhere


def test_citation_links_resolve(
    client: TestClient, investigated: tuple[Incident, Proposal]
) -> None:
    """The load-bearing property: a cited chunk must actually be reachable."""
    incident, _ = investigated
    body = client.get(f"/dashboard/incidents/{incident.key}").text

    import re

    links = set(re.findall(r'href="(/dashboard/kb/[^"]+)"', body))
    assert links, "no knowledge-base citations were linked"
    for link in links:
        assert client.get(link).status_code == 200, f"dead citation link: {link}"


def test_detail_shows_current_system_state(
    client: TestClient, investigated: tuple[Incident, Proposal]
) -> None:
    """Whether the system is still broken, not only what was said about it."""
    incident, _ = investigated
    body = client.get(f"/dashboard/incidents/{incident.key}").text
    assert "breaching" in body
    assert "12.40%" in body  # the real derived error rate
    assert "gateway_timeout_too_low" in body  # the active fault


def test_detail_renders_the_timeline(
    client: TestClient, investigated: tuple[Incident, Proposal]
) -> None:
    incident, _ = investigated
    body = client.get(f"/dashboard/incidents/{incident.key}").text
    assert "investigation started" in body
    assert "evidence gathered" in body
    assert "6 tool calls, 2 documents" in body


def test_detail_shows_executions_once_they_exist(
    client: TestClient, db: Session, investigated: tuple[Incident, Proposal]
) -> None:
    incident, proposal = investigated
    execution = repo.record_execution(
        db, incident, proposal, action_index=0, kind="set_config_value", params={}
    )
    repo.finish_execution(
        db,
        execution,
        status=ExecutionStatus.SUCCEEDED,
        summary="Set gateway_timeout_ms 1000 → 2000",
    )
    db.commit()

    body = client.get(f"/dashboard/incidents/{incident.key}").text
    assert "What was applied" in body
    assert "1000 → 2000" in body


def test_detail_without_a_proposal_says_so(client: TestClient, db: Session) -> None:
    triaged = service.triage_scenario(
        db, "login_outage_token_expiry", channel_id=CHANNEL, actor="U1"
    )
    db.commit()
    body = client.get(f"/dashboard/incidents/{triaged.incident.key}").text
    assert "No proposal yet" in body


def test_unknown_incident_is_404(client: TestClient) -> None:
    assert client.get("/dashboard/incidents/INC-NOPE").status_code == 404


def test_incident_key_is_case_insensitive(
    client: TestClient, investigated: tuple[Incident, Proposal]
) -> None:
    incident, _ = investigated
    assert client.get(f"/dashboard/incidents/{incident.key.lower()}").status_code == 200


# -- knowledge base ---------------------------------------------------------


def test_kb_index_lists_both_corpora(client: TestClient) -> None:
    body = client.get("/dashboard/kb").text
    assert "Runbooks" in body and "Resolved incident history" in body
    assert "payments-api upstream gateway failures" in body
    assert "INC-2025-0412" in body
    assert "set_config_value" in body  # how the historical incident was resolved


def test_chunk_page_shows_content_and_source(client: TestClient) -> None:
    body = client.get(f"/dashboard/kb/{CHUNK}").text
    assert "seed/kb/runbook-payments-gateway.md" in body
    # Rendered verbatim, backticks and all — the page shows the source text, not a re-write.
    assert "Prefer restoring `gateway_timeout_ms` to 2000." in body
    assert "enable_new_gateway" in body  # tags


def test_chunk_page_lists_the_incidents_that_used_it(
    client: TestClient, investigated: tuple[Incident, Proposal]
) -> None:
    """The reverse index — a knowledge base with a usage record, not a static pile."""
    incident, _ = investigated
    body = client.get(f"/dashboard/kb/{CHUNK}").text
    assert "Cited by" in body
    assert incident.key in body


def test_unused_chunk_has_no_cited_by_section(client: TestClient) -> None:
    body = client.get("/dashboard/kb/kb:template-comms:tone").text
    assert "Cited by" not in body


def test_unknown_chunk_is_404(client: TestClient) -> None:
    assert client.get("/dashboard/kb/kb:does-not-exist").status_code == 404


# -- scenarios --------------------------------------------------------------


def test_scenarios_page_shows_live_derived_metrics(client: TestClient) -> None:
    body = client.get("/dashboard/scenarios").text
    assert "payments_gateway_timeout" in body
    assert "12.40%" in body  # derived, not stored
    assert "breaching" in body
    assert "within SLO" in body  # noisy_neighbor is healthy


def test_dashboard_is_read_only(client: TestClient) -> None:
    """Approval lives in Slack, where the tokens are. Nothing here mutates."""
    for path in ("/dashboard", "/dashboard/kb", "/dashboard/scenarios"):
        assert client.post(path).status_code in {404, 405}


def test_dashboard_is_absent_from_the_public_api_schema(client: TestClient) -> None:
    """An internal ops view should not clutter the documented API surface."""
    schema: dict[str, Any] = client.get("/openapi.json").json()
    assert not [p for p in schema["paths"] if p.startswith("/dashboard")]
