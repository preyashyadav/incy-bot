"""Posting into Slack from outside a Slack request.

The worker has no Slack request to reply to — it finished the work minutes after the interaction
was acknowledged — so it posts through a `Notifier` instead.

Three implementations, chosen by configuration rather than by branching at each call site:

* `SlackNotifier` — the real thing.
* `NullNotifier` — used when no bot token is configured. The system stays fully functional
  without Slack; incidents run through the API and the timeline, and posts are dropped. That is
  what lets the whole test suite and a local demo work with no workspace at all.
* `RecordingNotifier` — a test double that keeps what would have been posted.

Failures never propagate. A Slack outage must not fail a job that already remediated the
incident, and an at-least-once retry would then re-run the remediation. Posting is best-effort
and the timeline remains the source of truth.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from incident_copilot.config import Settings, get_settings

logger = logging.getLogger(__name__)

Block = dict[str, Any]


class Notifier(Protocol):
    def post(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[Block] | None = None,
    ) -> str | None:
        """Post a message. Returns its `ts`, or None if it was not delivered."""
        ...

    def update(
        self, channel: str, ts: str, text: str, *, blocks: list[Block] | None = None
    ) -> None:
        """Replace an existing message — used to retire a card once it has been acted on."""
        ...


class NullNotifier:
    """Drops everything. The default when Slack is not configured."""

    def post(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[Block] | None = None,
    ) -> str | None:
        logger.debug("slack disabled; would post to %s: %s", channel, text[:120])
        return None

    def update(
        self, channel: str, ts: str, text: str, *, blocks: list[Block] | None = None
    ) -> None:
        logger.debug("slack disabled; would update %s/%s", channel, ts)


class RecordingNotifier:
    """Captures posts for assertions. Test support only."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self._counter = 0

    def post(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[Block] | None = None,
    ) -> str | None:
        self._counter += 1
        ts = f"17000000{self._counter:02d}.000100"
        self.posts.append(
            {"channel": channel, "text": text, "thread_ts": thread_ts, "blocks": blocks, "ts": ts}
        )
        return ts

    def update(
        self, channel: str, ts: str, text: str, *, blocks: list[Block] | None = None
    ) -> None:
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    # -- assertions --------------------------------------------------------

    def texts(self) -> list[str]:
        return [p["text"] for p in self.posts]

    def in_thread(self, thread_ts: str) -> list[dict[str, Any]]:
        return [p for p in self.posts if p["thread_ts"] == thread_ts]


class SlackNotifier:
    """Posts through the Slack Web API."""

    def __init__(self, token: str) -> None:
        from slack_sdk import WebClient

        self._client = WebClient(token=token)

    def post(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[Block] | None = None,
    ) -> str | None:
        try:
            response = self._client.chat_postMessage(
                channel=channel,
                text=text,  # always set: this is the notification and accessibility fallback
                thread_ts=thread_ts,
                blocks=blocks,
            )
            return str(response["ts"])
        except Exception:
            logger.exception("failed to post to slack channel %s", channel)
            return None

    def update(
        self, channel: str, ts: str, text: str, *, blocks: list[Block] | None = None
    ) -> None:
        try:
            self._client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
        except Exception:
            logger.exception("failed to update slack message %s/%s", channel, ts)


_notifier: Notifier | None = None


def build_notifier(settings: Settings | None = None) -> Notifier:
    settings = settings or get_settings()
    if settings.slack_bot_token is None:
        logger.info("no SLACK_BOT_TOKEN; slack notifications disabled")
        return NullNotifier()
    return SlackNotifier(settings.slack_bot_token.get_secret_value())


def get_notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = build_notifier()
    return _notifier


def set_notifier(notifier: Notifier | None) -> None:
    """Override the process-wide notifier. Test support only."""
    global _notifier
    _notifier = notifier
