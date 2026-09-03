"""Transport: the two adapters, and the request verification the HTTP one depends on.

The signature tests drive Bolt's real verifier with real HMAC signatures rather than asserting
that a config value is set. Under Socket Mode there is nothing to verify — the WebSocket is
authenticated once — so this is exactly the property that only exists in the transport we would
actually deploy, and the one worth proving.
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

from incident_copilot.config import Settings
from incident_copilot.slack.app import build_app, build_asgi_handler

SIGNING_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
BOT_TOKEN = "xoxb-test-token"
APP_TOKEN = "xapp-test-token"


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "slack_mode": "http",
        "slack_bot_token": BOT_TOKEN,
        "slack_signing_secret": SIGNING_SECRET,
        "slack_app_token": APP_TOKEN,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _fake_response(client: Any, data: dict[str, Any]) -> Any:
    """A real SlackResponse, because Bolt reads `.headers` off auth.test results."""
    from slack_sdk.web import SlackResponse

    return SlackResponse(
        client=client,
        http_verb="POST",
        api_url="https://slack.com/api/stub",
        req_args={},
        data=data,
        headers={},
        status_code=200,
    )


@pytest.fixture
def stub_slack(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Stub every Slack Web API call, recording what would have been sent.

    Without this, dispatching an interaction would make real network calls — and a test suite
    that talks to Slack is neither hermetic nor safe to run in CI.
    """
    from slack_sdk.web import WebClient

    calls: list[tuple[str, dict[str, Any]]] = []

    def record(name: str, payload: dict[str, Any]) -> Any:
        return lambda self, **kwargs: (
            calls.append((name, kwargs)),
            _fake_response(self, payload),
        )[1]

    monkeypatch.setattr(
        WebClient,
        "auth_test",
        record("auth_test", {"ok": True, "user_id": "U0BOT", "bot_id": "B0BOT"}),
    )
    monkeypatch.setattr(
        WebClient,
        "chat_postMessage",
        record("chat_postMessage", {"ok": True, "ts": "1700000000.000200"}),
    )
    monkeypatch.setattr(WebClient, "chat_postEphemeral", record("chat_postEphemeral", {"ok": True}))
    monkeypatch.setattr(WebClient, "chat_update", record("chat_update", {"ok": True}))
    monkeypatch.setattr(WebClient, "views_open", record("views_open", {"ok": True}))

    # `respond()` in a slash-command handler POSTs to the response_url, which is a different
    # client entirely — and an unstubbed one resolves a hostname at test time.
    from slack_sdk.webhook.client import WebhookClient, WebhookResponse

    def send_dict(self: Any, body: dict[str, Any], headers: Any = None) -> WebhookResponse:
        calls.append(("respond", body))
        return WebhookResponse(url="", status_code=200, body="ok", headers={})

    monkeypatch.setattr(WebhookClient, "send_dict", send_dict)
    yield calls


@pytest.fixture
def app(stub_slack: list[tuple[str, dict[str, Any]]], db: Any) -> Iterator[Any]:
    """A Bolt app that never reaches the network, against a clean database."""
    yield build_app(settings())


def sign(body: str, secret: str = SIGNING_SECRET, timestamp: int | None = None) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    basestring = f"v0:{ts}:{body}".encode()
    signature = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": signature}


def command_body(text: str = "list") -> str:
    return urlencode(
        {
            "token": "verification-token",
            "team_id": "T0TEAM",
            "channel_id": "C0CHANNEL",
            "user_id": "U0USER",
            "command": "/incident",
            "text": text,
            "response_url": "https://hooks.slack.example/commands/1234",
            "trigger_id": "123.456.abc",
        }
    )


def request_for(body: str, headers: dict[str, str]) -> BoltRequest:
    return BoltRequest(
        body=body,
        headers={
            **{k: [v] for k, v in headers.items()},
            "content-type": ["application/x-www-form-urlencoded"],
        },
    )


# -- construction -----------------------------------------------------------


def test_app_builds_in_http_mode(app: Any) -> None:
    assert app is not None


def test_app_builds_in_socket_mode(stub_slack: list[tuple[str, dict[str, Any]]]) -> None:
    """Same handlers, different transport — that is the whole point of the abstraction."""
    assert build_app(settings(slack_mode="socket", slack_signing_secret=None)) is not None


def test_http_mode_requires_a_signing_secret() -> None:
    """Without it there is no way to tell a real Slack request from anyone's POST."""
    with pytest.raises(RuntimeError, match="SLACK_SIGNING_SECRET"):
        build_app(settings(slack_signing_secret=None))


def test_socket_mode_requires_an_app_token() -> None:
    with pytest.raises(RuntimeError, match="SLACK_APP_TOKEN"):
        build_app(settings(slack_mode="socket", slack_app_token=None))


def test_a_bot_token_is_always_required() -> None:
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        build_app(settings(slack_bot_token=None))


def test_asgi_handler_is_constructible(stub_slack: list[tuple[str, dict[str, Any]]]) -> None:
    assert build_asgi_handler(settings()) is not None


# -- signature verification -------------------------------------------------


def test_a_correctly_signed_request_is_accepted(app: Any) -> None:
    body = command_body()
    response = app.dispatch(request_for(body, sign(body)))
    assert response.status == 200


def test_an_unsigned_request_is_rejected(app: Any) -> None:
    """The bare minimum: anyone can POST to a public URL."""
    response = app.dispatch(request_for(command_body(), {}))
    assert response.status == 401


def test_a_wrong_signature_is_rejected(app: Any) -> None:
    body = command_body()
    response = app.dispatch(request_for(body, sign(body, secret="the-wrong-secret")))
    assert response.status == 401


def test_a_tampered_body_is_rejected(app: Any) -> None:
    """The signature covers the body, so rewriting the command after signing must fail."""
    headers = sign(command_body("list"))
    response = app.dispatch(request_for(command_body("triage payments_gateway_timeout"), headers))
    assert response.status == 401


def test_a_stale_timestamp_is_rejected(app: Any) -> None:
    """Replay protection: a captured request must not stay valid indefinitely."""
    body = command_body()
    old = int(time.time()) - 60 * 60
    response = app.dispatch(request_for(body, sign(body, timestamp=old)))
    assert response.status == 401


def test_a_future_timestamp_is_rejected(app: Any) -> None:
    body = command_body()
    future = int(time.time()) + 60 * 60
    response = app.dispatch(request_for(body, sign(body, timestamp=future)))
    assert response.status == 401


# -- routing ----------------------------------------------------------------


def test_registered_actions_cover_the_card_buttons(app: Any) -> None:
    """A button whose action_id has no listener silently does nothing when clicked.

    Asserted by dispatch rather than by introspecting Bolt's listener registry, which is
    private and stores matchers as closures.
    """
    for action_id in (
        "investigate_incident",
        "ignore_alert",
        "approve_proposal",
        "reject_proposal",
        "explain_proposal",
    ):
        body = urlencode(
            {
                "payload": json.dumps(
                    {
                        "type": "block_actions",
                        "user": {"id": "U0USER"},
                        "channel": {"id": "C0CHANNEL"},
                        "message": {"ts": "1700000000.000100"},
                        "trigger_id": f"trigger-{action_id}",
                        "actions": [{"action_id": action_id, "type": "button", "value": "{}"}],
                    }
                )
            }
        )
        response = app.dispatch(request_for(body, sign(body)))
        # 200 means a listener matched and ran; 404 would mean the button is wired to nothing.
        assert response.status == 200, f"no listener for {action_id}"


def test_unknown_action_is_not_an_error(app: Any) -> None:
    """An unrecognised interaction is answered 404, not raised.

    Socket Mode delivers every event the subscription allows, so unhandled requests are normal
    rather than exceptional — `raise_error_for_unhandled_request=False` keeps them off the
    error path while still not pretending they were handled.
    """
    body = urlencode(
        {
            "payload": json.dumps(
                {
                    "type": "block_actions",
                    "user": {"id": "U0USER"},
                    "channel": {"id": "C0CHANNEL"},
                    "message": {"ts": "1700000000.000100"},
                    "trigger_id": "t",
                    "actions": [{"action_id": "nope", "type": "button", "value": "{}"}],
                }
            )
        }
    )
    assert app.dispatch(request_for(body, sign(body))).status == 404
