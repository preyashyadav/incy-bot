"""Block Kit rendering.

Slack rejects a malformed message outright, and a card that fails to render during an incident
is a card nobody can approve. These tests check the structural rules Slack enforces, plus the
one security property the blocks carry: buttons leak nothing and authorise nothing on their own.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session

from incident_copilot.db import repositories as repo
from incident_copilot.db.models import ApprovalToken, Incident, Proposal
from incident_copilot.slack import blocks as B
from incident_copilot.slack import service

CHANNEL = "C0INCIDENT"


@pytest.fixture
def triaged(db: Session) -> Iterator[service.TriagedIncident]:
    result = service.triage_scenario(
        db, "payments_gateway_timeout", channel_id=CHANNEL, actor="U0PREYASH"
    )
    db.commit()
    yield result


@pytest.fixture
def proposal_bundle(
    db: Session, triaged: service.TriagedIncident
) -> tuple[Incident, Proposal, ApprovalToken, ApprovalToken]:
    proposal = repo.create_proposal(
        db,
        triaged.incident,
        severity="SEV1",
        hypothesis="gateway_timeout_ms was lowered below the new client's floor.",
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
        evidence_cited=["get_metrics", "kb:runbook-payments-gateway:mitigation"],
        similar_incidents=["INC-2025-0412"],
        verification_plan="error_rate returns below 1%.",
        raw={"ruled_out": ["Deploy v2.4.1 — ships both clients"], "requires_followup": False},
    )
    approve = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=1800)
    reject = repo.issue_approval_token(db, proposal, decision="reject", ttl_seconds=1800)
    db.commit()
    return triaged.incident, proposal, approve, reject


def walk(blocks: list[dict[str, Any]]) -> str:
    """Every string in a block payload, for leak checks."""
    import json

    return json.dumps(blocks)


def buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        element
        for block in blocks
        if block["type"] == "actions"
        for element in block["elements"]
        if element["type"] == "button"
    ]


# -- token payloads ---------------------------------------------------------


def test_button_payload_is_only_a_token() -> None:
    """A button value is a request parameter the client controls, not trusted state."""
    value = B.token_value("abc123")
    assert B.read_token(value) == "abc123"
    assert set(__import__("json").loads(value)) == {"t"}


@pytest.mark.parametrize(
    "value",
    [None, "", "not json", "[]", '"a string"', "{}", '{"t": 42}', '{"other": "x"}'],
)
def test_malformed_button_payloads_yield_no_token(value: str | None) -> None:
    """Parsing untrusted input must never raise — a bad value is simply unauthorised."""
    assert B.read_token(value) is None


# -- alert card -------------------------------------------------------------


def test_alert_card_structure(triaged: service.TriagedIncident) -> None:
    payload = B.alert_blocks(
        triaged.incident,
        signal="error_rate_spike",
        impact="Customers cannot check out",
        token=triaged.investigate_token.token,
    )
    assert payload[0]["type"] == "header"
    assert {b["action_id"] for b in buttons(payload)} == {"investigate_incident", "ignore_alert"}
    assert triaged.incident.key in walk(payload)


def test_alert_card_leaks_no_internal_identifiers(triaged: service.TriagedIncident) -> None:
    """Ids in a button payload would be an invitation to tamper with them."""
    payload = B.alert_blocks(
        triaged.incident, signal="s", impact="i", token=triaged.investigate_token.token
    )
    for button in buttons(payload):
        assert str(triaged.incident.id) not in button["value"]
        assert triaged.incident.scenario_key not in button["value"]


def test_alert_text_fallback_is_meaningful(triaged: service.TriagedIncident) -> None:
    """This is the notification and the screen-reader text; a blocks-only message is silent."""
    text = B.alert_text(triaged.incident)
    assert triaged.incident.severity in text
    assert triaged.incident.title[:20] in text


# -- proposal card ----------------------------------------------------------


def test_proposal_card_shows_the_reasoning(
    proposal_bundle: tuple[Incident, Proposal, ApprovalToken, ApprovalToken],
) -> None:
    incident, proposal, approve, reject = proposal_bundle
    payload = B.proposal_blocks(incident, proposal, approve_token=approve, reject_token=reject)
    rendered = walk(payload)

    assert "What I think is happening" in rendered
    assert proposal.hypothesis in rendered
    assert "Ruled out" in rendered
    assert "gateway_timeout_ms" in rendered
    assert "INC-2025-0412" in rendered  # the precedent is visible to the approver
    assert "How we'll know it worked" in rendered


def test_proposal_card_marks_risk_and_reversibility(
    proposal_bundle: tuple[Incident, Proposal, ApprovalToken, ApprovalToken],
) -> None:
    """The approver is accountable for the change, so its risk has to be on the card."""
    incident, proposal, approve, reject = proposal_bundle
    rendered = walk(
        B.proposal_blocks(incident, proposal, approve_token=approve, reject_token=reject)
    )
    assert "low risk" in rendered
    assert "reversible" in rendered


def test_proposal_card_carries_a_confirmation_dialog(
    proposal_bundle: tuple[Incident, Proposal, ApprovalToken, ApprovalToken],
) -> None:
    """Cheap insurance against a misclick in a busy channel."""
    incident, proposal, approve, reject = proposal_bundle
    payload = B.proposal_blocks(incident, proposal, approve_token=approve, reject_token=reject)
    approve_button = next(b for b in buttons(payload) if b["action_id"] == "approve_proposal")
    assert "confirm" in approve_button
    assert "gateway_timeout_ms" in walk([approve_button])


def test_approve_and_reject_carry_different_tokens(
    proposal_bundle: tuple[Incident, Proposal, ApprovalToken, ApprovalToken],
) -> None:
    """The token, not the button, decides — so they must not be interchangeable."""
    incident, proposal, approve, reject = proposal_bundle
    payload = B.proposal_blocks(incident, proposal, approve_token=approve, reject_token=reject)
    by_id = {b["action_id"]: B.read_token(b["value"]) for b in buttons(payload)}
    assert by_id["approve_proposal"] == approve.token
    assert by_id["reject_proposal"] == reject.token
    assert by_id["approve_proposal"] != by_id["reject_proposal"]


def test_no_action_proposal_renders_differently(
    db: Session, triaged: service.TriagedIncident
) -> None:
    """'Nothing is wrong' must not look like a pending change, or it invites a needless click."""
    proposal = repo.create_proposal(
        db,
        triaged.incident,
        severity="SEV3",
        hypothesis="Traffic surge already absorbed by the autoscaler.",
        confidence="high",
        actions=[
            {
                "kind": "no_action",
                "reason": "All metrics inside SLO.",
                "rationale": "Autoscaler responded.",
                "risk": "low",
                "reversible": True,
            }
        ],
        verification_plan="No change expected.",
    )
    approve = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=60)
    reject = repo.issue_approval_token(db, proposal, decision="reject", ttl_seconds=60)
    db.commit()

    payload = B.proposal_blocks(
        triaged.incident, proposal, approve_token=approve, reject_token=reject
    )
    rendered = walk(payload)
    assert "No action needed" in rendered
    assert "Take no action" in rendered

    approve_button = next(b for b in buttons(payload) if b["action_id"] == "approve_proposal")
    assert "Confirm no action" in approve_button["text"]["text"]
    # Nothing changes, so there is nothing to confirm.
    assert "confirm" not in approve_button


def test_followup_warning_is_shown(db: Session, triaged: service.TriagedIncident) -> None:
    """A restart against a leak must not read as 'resolved'."""
    proposal = repo.create_proposal(
        db,
        triaged.incident,
        severity="SEV2",
        hypothesis="Unbounded cart cache.",
        confidence="high",
        actions=[
            {
                "kind": "restart_service",
                "service": "checkout-api",
                "rationale": "Clears the heap.",
                "risk": "low",
                "reversible": True,
            }
        ],
        raw={"requires_followup": True, "followup_note": "Bound the cart cache."},
    )
    approve = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=60)
    reject = repo.issue_approval_token(db, proposal, decision="reject", ttl_seconds=60)
    db.commit()

    rendered = walk(
        B.proposal_blocks(triaged.incident, proposal, approve_token=approve, reject_token=reject)
    )
    assert "Mitigation only" in rendered
    assert "Bound the cart cache." in rendered


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ({"kind": "rollback_deploy", "service": "api"}, "Roll back"),
        ({"kind": "toggle_feature_flag", "flag": "f", "enabled": False}, "Disable"),
        ({"kind": "toggle_feature_flag", "flag": "f", "enabled": True}, "Enable"),
        ({"kind": "set_config_value", "key": "k", "value": 1}, "Set"),
        ({"kind": "scale_replicas", "service": "api", "replicas": 8}, "Scale"),
        ({"kind": "restart_service", "service": "api"}, "Restart"),
        ({"kind": "no_action"}, "Take no action"),
        ({"kind": "something_new"}, "something_new"),
    ],
)
def test_every_action_kind_describes_itself(action: dict[str, Any], expected: str) -> None:
    assert expected in B.describe_action(action)


def test_long_text_is_clipped_below_slacks_limit(
    db: Session, triaged: service.TriagedIncident
) -> None:
    """Slack rejects a section over 3000 characters, taking the whole card with it."""
    proposal = repo.create_proposal(
        db,
        triaged.incident,
        severity="SEV1",
        hypothesis="x" * 6000,
        confidence="low",
        actions=[{"kind": "no_action", "rationale": "y" * 6000, "risk": "low", "reversible": True}],
    )
    approve = repo.issue_approval_token(db, proposal, decision="approve", ttl_seconds=60)
    reject = repo.issue_approval_token(db, proposal, decision="reject", ttl_seconds=60)
    db.commit()

    payload = B.proposal_blocks(
        triaged.incident, proposal, approve_token=approve, reject_token=reject
    )
    for block in payload:
        if block["type"] == "section" and "text" in block:
            assert len(block["text"]["text"]) <= 3000
        for element in block.get("elements", []):
            if element.get("type") == "mrkdwn":
                assert len(element["text"]) <= 3000


# -- modal and timeline -----------------------------------------------------


def test_explain_modal_shows_the_audit_trail(
    db: Session, proposal_bundle: tuple[Incident, Proposal, ApprovalToken, ApprovalToken]
) -> None:
    """What turns a proposal from an assertion into something checkable."""
    incident, proposal, _, _ = proposal_bundle
    from incident_copilot.db.models import EventType

    repo.append_event(
        db,
        incident,
        EventType.EVIDENCE_GATHERED,
        {
            "tool_calls": 6,
            "transcript": ["get_metrics()", "search_runbooks(gateway)"],
            "cited_chunks": ["kb:runbook-payments-gateway:mitigation"],
            "input_tokens": 12000,
            "output_tokens": 900,
        },
    )
    db.commit()

    view = B.explain_modal(incident, proposal, repo.timeline(db, incident))
    assert view["type"] == "modal"
    rendered = walk(view["blocks"])
    assert "get_metrics()" in rendered
    assert "kb:runbook-payments-gateway:mitigation" in rendered
    assert "12,000" in rendered  # token accounting is visible


def test_explain_modal_survives_a_missing_evidence_event(
    proposal_bundle: tuple[Incident, Proposal, ApprovalToken, ApprovalToken],
) -> None:
    incident, proposal, _, _ = proposal_bundle
    view = B.explain_modal(incident, proposal, [])
    assert view["blocks"]


def test_timeline_renders_every_event(db: Session, triaged: service.TriagedIncident) -> None:
    from incident_copilot.db.models import EventType

    repo.append_event(db, triaged.incident, EventType.INVESTIGATION_STARTED)
    repo.append_event(db, triaged.incident, EventType.EVIDENCE_GATHERED, {"tool_calls": 6})
    db.commit()

    rendered = walk(B.timeline_blocks(triaged.incident, repo.timeline(db, triaged.incident)))
    assert triaged.incident.key in rendered
    assert "investigation started" in rendered
    assert "6 tools" in rendered


def test_decided_card_removes_the_buttons(
    db: Session, proposal_bundle: tuple[Incident, Proposal, ApprovalToken, ApprovalToken]
) -> None:
    """Once decided, the card must stop inviting a second click."""
    _, proposal, _, _ = proposal_bundle
    repo.decide_proposal(db, proposal, approved=True, actor="U0PREYASH")
    db.commit()

    payload = B.decided_blocks(proposal, approved=True, user_id="U0PREYASH")
    assert not buttons(payload)
    assert "approved" in walk(payload)


def test_scenario_picker_lists_scenarios() -> None:
    rendered = walk(B.scenario_picker_blocks(service.scenario_catalogue()))
    assert "payments_gateway_timeout" in rendered
    assert "/incident triage" in rendered
