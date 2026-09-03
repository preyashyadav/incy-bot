"""The Bolt application, and the two transports it can run under.

Socket Mode and HTTP are not competing designs — they are the same handlers reached two ways,
and which one is right depends on where the process is running:

* **HTTP** is the production path. Slack POSTs to a public URL, so replicas are stateless and
  scale horizontally behind a load balancer, Slack retries on any non-2xx, and the request is
  visible to normal HTTP tooling — access logs, traces, rate limiting. It is also the only
  transport eligible for app distribution.
* **Socket Mode** is the development path. The app dials out over a WebSocket, so it needs no
  public URL and no tunnel. That convenience costs the things above: connections are stateful,
  there is a per-app limit on them, and events are dropped rather than retried while the socket
  is down.

Committing to one would be the wrong call. Bolt abstracts the transport, so `SLACK_MODE` picks
at startup and the handler code below never knows the difference.
"""

from __future__ import annotations

import logging
from typing import Any

from slack_bolt import App

from incident_copilot.config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_app(settings: Settings | None = None) -> App:
    """Construct the Bolt app with handlers registered."""
    settings = settings or get_settings()
    settings.require_slack_credentials()

    assert settings.slack_bot_token is not None
    app = App(
        token=settings.slack_bot_token.get_secret_value(),
        # Socket Mode authenticates the WebSocket itself, so no signing secret is involved;
        # under HTTP, Bolt verifies every inbound request's signature and timestamp with it.
        signing_secret=(
            settings.slack_signing_secret.get_secret_value()
            if settings.slack_signing_secret is not None
            else None
        ),
        # Bolt's own warning about unhandled requests is noisy in Socket Mode, where the app
        # receives every event type the subscription allows.
        raise_error_for_unhandled_request=False,
        process_before_response=settings.slack_mode == "http",
    )

    from incident_copilot.slack import handlers

    handlers.register(app)
    return app


def run_socket_mode(settings: Settings | None = None) -> None:
    """Block, serving Slack over a WebSocket. Development entry point."""
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    settings = settings or get_settings()
    app = build_app(settings)
    assert settings.slack_app_token is not None
    logger.info("starting Slack in socket mode")
    # slack_bolt ships no type information for the adapter's blocking start().
    SocketModeHandler(app, settings.slack_app_token.get_secret_value()).start()  # type: ignore[no-untyped-call]


def build_asgi_handler(settings: Settings | None = None) -> Any:
    """The FastAPI-mountable request handler. Production entry point."""
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    return SlackRequestHandler(build_app(settings or get_settings()))


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
    )
    if settings.slack_mode != "socket":
        raise SystemExit(
            "SLACK_MODE is 'http'; the Bolt handler is served by the API process. "
            "Run `make api` instead, or set SLACK_MODE=socket."
        )
    run_socket_mode(settings)


if __name__ == "__main__":
    main()
