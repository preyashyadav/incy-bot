"""The whole Slack loop, driven through Bolt.

Signed requests go in at the transport, real handlers run, the real database records what
happened, and the only things faked are Slack's own API and the model's choices. This is the
test that would catch a card wired to the wrong `action_id`, a token that never reaches the
service layer, or a redelivery that silently doubles the work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
from slack_bolt.request import BoltRequest
from sqlalchemy.orm import Session

from incident_copilot.config import Settings
from incident_copilot.controlplane.pg_store import PostgresControlPlaneStore
from incident_copilot.db.models import (
    ApprovalToken,
    Incident,
    IncidentStatus,
    Job,
    Proposal,
    ProposalStatus,
)
from incident_copilot.jobs.handlers import investigate as investigate_handler
from incident_copilot.retrieval.index import reindex
from incident_copilot.slack import blocks as B
from incident_copilot.slack.app import build_app
from incident_copilot.slack.notify import RecordingNotifier, set_notifier
from tests.fake_anthropic import FakeAnthropic, make_proposal

SIGNING_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
CHANNEL = "C0INCIDENT"
USER = "U0PREYASH"


def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        slack_mode="http",
        slack_bot_token="xoxb-test",
        slack_signing_secret=SIGNING_SECRET,
    )


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    from slack_sdk.web import SlackResponse, WebClient
    from slack_sdk.webhook.client import WebhookClient, WebhookResponse

    calls: list[tuple[str, dict[str, Any]]] = []
    counter = {"n": 0}

    def record(name: str, extra: dict[str, Any] | None = None) -> Any:
        def call(self: Any, **kwargs: Any) -> SlackResponse:
            counter["n"] += 1
            calls.append((name, kwargs))
            return SlackResponse(
                client=self,
                http_verb="POST",
                api_url="https://slack.com/api/stub",
                req_args={},
                data={"ok": True, "ts": f"170000000{counter['n']}.000100", **(extra or {})},
                headers={},
                status_code=200,
            )

        return call

    for method in (
        "auth_test",
        "chat_postMessage",
        "chat_postEphemeral",
        "chat_update",
        "views_open",
    ):
        monkeypatch.setattr(WebClient, method, record(method))

    def send_dict(self: Any, body: dict[str, Any], headers: Any = None) -> WebhookResponse:
        calls.append(("respond", body))
        return WebhookResponse(url="", status_code=200, body="ok", headers={})

    monkeypatch.setattr(WebhookClient, "send_dict", send_dict)
    yield calls


@pytest.fixture
def notifier() -> Iterator[RecordingNotifier]:
    recorder = RecordingNotifier()
    set_notifier(recorder)
    yield recorder
    set_notifier(None)


@pytest.fixture
def app(slack: list[tuple[str, dict[str, Any]]], db: Session) -> Iterator[Any]:
    reindex(db)
    db.commit()
    yield build_app(settings())


def sign(body: str) -> dict[str, list[str]]:
    ts = str(int(time.time()))
    signature = (
        "v0="
        + hmac.new(SIGNING_SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    )
    return {
        "x-slack-request-timestamp": [ts],
        "x-slack-signature": [signature],
        "content-type": ["application/x-www-form-urlencoded"],
    }


def send(app: Any, body: str) -> Any:
    return app.dispatch(BoltRequest(body=body, headers=sign(body)))


def command(text: str, *, response_url: str = "https://hooks.slack.example/c/1") -> str:
    return urlencode(
        {
            "channel_id": CHANNEL,
            "user_id": USER,
            "command": "/incident",
            "text": text,
            "response_url": response_url,
            "trigger_id": f"trigger-{text}",
        }
    )


def click(
    action_id: str, token: str, *, trigger: str, message_ts: str = "1700000001.000100"
) -> str:
    return urlencode(
        {
            "payload": json.dumps(
                {
                    "type": "block_actions",
                    "user": {"id": USER},
                    "channel": {"id": CHANNEL},
                    "message": {"ts": message_ts},
                    "trigger_id": trigger,
                    "actions": [
                        {
                            "action_id": action_id,
                            "type": "button",
                            "value": B.token_value(token),
                        }
                    ],
                }
            )
        }
    )


def posted(calls: list[tuple[str, dict[str, Any]]], method: str) -> list[dict[str, Any]]:
    return [payload for name, payload in calls if name == method]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_triage_posts_an_alert_card(app: Any, db: Session, slack: Any) -> None:
    assert send(app, command("triage payments_gateway_timeout")).status == 200

    incident = db.query(Incident).one()
    assert incident.scenario_key == "payments_gateway_timeout"
    assert incident.slack_channel_id == CHANNEL
    # The thread root is recorded from the posted message, so later updates find it.
    assert incident.slack_thread_ts is not None

    message = posted(slack, "chat_postMessage")[0]
    assert message["channel"] == CHANNEL
    assert message["text"], "a card with no text fallback is a silent notification"
    action_ids = {
        element["action_id"]
        for block in message["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
    }
    assert action_ids == {"investigate_incident", "ignore_alert"}


def test_triage_resets_the_scenario(app: Any, db: Session) -> None:
    """Two demos in a row must start from the same place."""
    send(app, command("triage payments_gateway_timeout"))
    state = PostgresControlPlaneStore().get_in(db, "payments_gateway_timeout")
    assert state.config["gateway_timeout_ms"].value == 1000


def test_investigate_button_queues_the_agent(app: Any, db: Session, slack: Any) -> None:
    send(app, command("triage payments_gateway_timeout"))
    token = db.query(ApprovalToken).one().token

    assert send(app, click("investigate_incident", token, trigger="t1")).status == 200

    job = db.query(Job).one()
    assert job.kind == "investigate"
    assert "started an investigation" in posted(slack, "chat_postMessage")[-1]["text"]


def test_full_loop_to_an_approved_remediation(
    app: Any, db: Session, slack: Any, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alert → investigate → proposal card → approve → remediation queued."""
    send(app, command("triage payments_gateway_timeout"))
    investigate_token = db.query(ApprovalToken).one().token
    send(app, click("investigate_incident", investigate_token, trigger="t1"))

    # Run the queued investigation with a scripted model.
    client = FakeAnthropic(proposal=make_proposal())
    original = investigate_handler.run_agent
    monkeypatch.setattr(
        investigate_handler,
        "run_agent",
        lambda s, sc, st, **kw: original(s, sc, st, client=client, **kw),
    )
    job = db.query(Job).filter(Job.kind == "investigate").one()
    investigate_handler.handle_investigate(db, job)
    db.commit()

    # The proposal card reached the incident thread.
    assert len(notifier.posts) == 1
    card = notifier.posts[0]
    assert card["channel"] == CHANNEL
    assert card["thread_ts"] is not None
    assert "gateway_timeout_ms" in json.dumps(card["blocks"])

    proposal = db.query(Proposal).filter(Proposal.status == ProposalStatus.PENDING).one()
    approve = (
        db.query(ApprovalToken)
        .filter(ApprovalToken.proposal_id == proposal.id, ApprovalToken.decision == "approve")
        .one()
    )

    assert send(app, click("approve_proposal", approve.token, trigger="t2")).status == 200
    db.expire_all()

    assert proposal.status is ProposalStatus.APPROVED
    assert proposal.decided_by == USER
    incident = db.query(Incident).one()
    assert incident.status is IncidentStatus.REMEDIATING
    assert db.query(Job).filter(Job.kind == "execute_remediation").count() == 1

    # The card is retired so nobody can approve it twice from the UI.
    assert posted(slack, "chat_update"), "the decided card was not replaced"


def test_rejection_changes_nothing(app: Any, db: Session, slack: Any) -> None:
    send(app, command("triage payments_gateway_timeout"))
    incident = db.query(Incident).one()
    proposal = incident.proposals[0]
    reject = db.query(ApprovalToken).filter(ApprovalToken.decision == "approve").one()
    # Re-issue as a reject token to exercise the reject path on the placeholder proposal.
    from incident_copilot.db import repositories as repo

    reject = repo.issue_approval_token(db, proposal, decision="reject", ttl_seconds=1800)
    db.commit()

    assert send(app, click("reject_proposal", reject.token, trigger="t3")).status == 200
    db.expire_all()

    assert proposal.status is ProposalStatus.REJECTED
    assert db.query(Job).filter(Job.kind == "execute_remediation").count() == 0
    assert "rejected" in posted(slack, "chat_postMessage")[-1]["text"]


# ---------------------------------------------------------------------------
# Abuse and failure
# ---------------------------------------------------------------------------


def test_a_forged_button_value_authorises_nothing(app: Any, db: Session, slack: Any) -> None:
    """The security property the whole token design exists for."""
    send(app, command("triage payments_gateway_timeout"))
    assert send(app, click("investigate_incident", "forged-token", trigger="t4")).status == 200

    assert db.query(Job).count() == 0
    assert posted(slack, "chat_postEphemeral"), "the user was not told it was refused"
    assert "no longer valid" in posted(slack, "chat_postEphemeral")[-1]["text"]


def test_a_redelivered_click_does_not_double_act(app: Any, db: Session) -> None:
    """Slack redelivers anything it does not get a 2xx for; the second must be a no-op."""
    send(app, command("triage payments_gateway_timeout"))
    token = db.query(ApprovalToken).one().token

    body = click("investigate_incident", token, trigger="same-trigger")
    assert send(app, body).status == 200
    assert send(app, body).status == 200  # identical delivery, replayed

    assert db.query(Job).count() == 1


def test_a_second_click_after_the_token_is_spent_is_refused(
    app: Any, db: Session, slack: Any
) -> None:
    """Distinct from a redelivery: a genuinely new interaction on a used token."""
    send(app, command("triage payments_gateway_timeout"))
    token = db.query(ApprovalToken).one().token

    send(app, click("investigate_incident", token, trigger="first"))
    send(app, click("investigate_incident", token, trigger="second"))

    assert db.query(Job).count() == 1
    assert "already been used" in posted(slack, "chat_postEphemeral")[-1]["text"]


def test_triage_of_an_unknown_scenario_is_reported(app: Any, db: Session, slack: Any) -> None:
    assert send(app, command("triage nonsense")).status == 200
    assert db.query(Incident).count() == 0
    assert any("Available" in json.dumps(p) for p in posted(slack, "respond"))


# ---------------------------------------------------------------------------
# Read-only surfaces
# ---------------------------------------------------------------------------


def test_status_returns_the_timeline(app: Any, db: Session, slack: Any) -> None:
    send(app, command("triage payments_gateway_timeout"))
    key = db.query(Incident).one().key

    assert (
        send(app, command(f"status {key}", response_url="https://hooks.slack.example/c/2")).status
        == 200
    )
    rendered = json.dumps(posted(slack, "respond")[-1])
    assert key in rendered
    assert "Timeline" in rendered


def test_status_of_an_unknown_incident_is_reported(app: Any, slack: Any) -> None:
    send(app, command("status INC-NOPE"))
    assert "No incident found" in json.dumps(posted(slack, "respond")[-1])


def test_list_shows_the_catalogue(app: Any, slack: Any) -> None:
    send(app, command("list"))
    assert "payments_gateway_timeout" in json.dumps(posted(slack, "respond")[-1])


def test_explicit_help_shows_usage(app: Any, slack: Any) -> None:
    send(app, command("help"))
    assert "/incident triage" in json.dumps(posted(slack, "respond")[-1])


def test_unknown_subcommand_shows_help(app: Any, slack: Any) -> None:
    send(app, command("frobnicate"))
    rendered = json.dumps(posted(slack, "respond")[-1])
    assert "Unknown subcommand" in rendered


def test_explain_opens_the_evidence_modal(
    app: Any, db: Session, slack: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    send(app, command("triage payments_gateway_timeout"))
    investigate_token = db.query(ApprovalToken).one().token
    send(app, click("investigate_incident", investigate_token, trigger="t1"))

    client = FakeAnthropic(proposal=make_proposal())
    original = investigate_handler.run_agent
    monkeypatch.setattr(
        investigate_handler,
        "run_agent",
        lambda s, sc, st, **kw: original(s, sc, st, client=client, **kw),
    )
    investigate_handler.handle_investigate(
        db, db.query(Job).filter(Job.kind == "investigate").one()
    )
    db.commit()

    proposal = db.query(Proposal).filter(Proposal.status == ProposalStatus.PENDING).one()
    body = urlencode(
        {
            "payload": json.dumps(
                {
                    "type": "block_actions",
                    "user": {"id": USER},
                    "channel": {"id": CHANNEL},
                    "message": {"ts": "1700000001.000100"},
                    "trigger_id": "trigger-explain",
                    "actions": [
                        {
                            "action_id": "explain_proposal",
                            "type": "button",
                            "value": json.dumps({"p": str(proposal.id)}),
                        }
                    ],
                }
            )
        }
    )
    assert send(app, body).status == 200

    opened = posted(slack, "views_open")
    assert opened, "the modal was never opened"
    rendered = json.dumps(opened[-1]["view"])
    assert "get_metrics()" in rendered  # the audit trail is what the modal is for


# ---------------------------------------------------------------------------
# Command text normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "triage payments_gateway_timeout",
        "`triage payments_gateway_timeout`",  # composed with code formatting on
        "  triage   payments_gateway_timeout  ",
        "triage payments_gateway_timeout",  # non-breaking space
        "​triage payments_gateway_timeout",  # zero-width space
        "/incident triage payments_gateway_timeout",  # command echoed into the argument
    ],
)
def test_command_text_is_normalised_before_parsing(app: Any, db: Session, raw: str) -> None:
    """A user who formatted their message oddly still meant `triage`.

    Regression guard: an unnormalised value fell through to the help branch, which looks like
    the bot ignoring the command and says nothing about why.
    """
    from incident_copilot.slack.handlers import normalise_command_text

    assert normalise_command_text(raw) == "triage payments_gateway_timeout"

    body = urlencode(
        {
            "channel_id": CHANNEL,
            "user_id": USER,
            "command": "/incident",
            "text": raw,
            "response_url": f"https://hooks.slack.example/c/{abs(hash(raw))}",
            "trigger_id": f"trigger-{abs(hash(raw))}",
        }
    )
    assert send(app, body).status == 200
    assert db.query(Incident).count() == 1, f"{raw!r} did not triage an incident"


@pytest.mark.parametrize("raw", [None, "", "   ", "`  `"])
def test_empty_command_text_still_means_help(raw: str | None) -> None:
    from incident_copilot.slack.handlers import normalise_command_text

    assert normalise_command_text(raw) == ""


def test_bare_command_offers_clickable_scenarios(app: Any, slack: Any) -> None:
    """Slack can deliver a command with empty text; the reply must still be actionable.

    Observed in the real workspace: `/incident triage payments_gateway_timeout` composed with
    rich-text formatting arrived as `text=''`. Telling the user to retype is not a fix when the
    retype can fail the same way.
    """
    send(app, command(""))
    rendered = posted(slack, "respond")[-1]
    accessories = [
        b["accessory"] for b in rendered["blocks"] if b.get("accessory", {}).get("type") == "button"
    ]
    assert len(accessories) >= 5, "the picker offered no buttons"
    assert {a["action_id"] for a in accessories} == {"triage_scenario"}
    assert "payments_gateway_timeout" in {a["value"] for a in accessories}


def test_triage_button_opens_an_incident(app: Any, db: Session, slack: Any) -> None:
    body = urlencode(
        {
            "payload": json.dumps(
                {
                    "type": "block_actions",
                    "user": {"id": USER},
                    "channel": {"id": CHANNEL},
                    "message": {"ts": "1700000001.000100"},
                    "trigger_id": "trigger-triage-button",
                    "actions": [
                        {
                            "action_id": "triage_scenario",
                            "type": "button",
                            "value": "payments_gateway_timeout",
                        }
                    ],
                }
            )
        }
    )
    assert send(app, body).status == 200

    incident = db.query(Incident).one()
    assert incident.scenario_key == "payments_gateway_timeout"
    assert incident.slack_thread_ts is not None
    assert posted(slack, "chat_postMessage"), "no alert card was posted"


def test_triage_button_rejects_an_unknown_scenario(app: Any, db: Session, slack: Any) -> None:
    """The button value is client-controlled, so it is validated against the registry."""
    body = urlencode(
        {
            "payload": json.dumps(
                {
                    "type": "block_actions",
                    "user": {"id": USER},
                    "channel": {"id": CHANNEL},
                    "message": {"ts": "1700000001.000100"},
                    "trigger_id": "trigger-bad-scenario",
                    "actions": [
                        {"action_id": "triage_scenario", "type": "button", "value": "../../etc"}
                    ],
                }
            )
        }
    )
    assert send(app, body).status == 200
    assert db.query(Incident).count() == 0
    assert "Available" in posted(slack, "chat_postEphemeral")[-1]["text"]
