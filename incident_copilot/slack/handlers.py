"""Slack handlers.

Every handler obeys the same shape:

1. **`ack()` first.** Slack allows three seconds, and it retries anything it does not get a 2xx
   for. Acknowledging late does not merely look slow — it produces duplicate deliveries.
2. **Check for a redelivery.** A retry is indistinguishable from a fresh click at this layer, so
   the delivery key is recorded under a unique constraint before any work happens.
3. **Do the smallest durable thing, then queue the rest.** Anything that can take longer than the
   ack budget — the agent, remediation, verification — becomes a job.

Handlers stay thin on purpose; the logic they call lives in `service.py`, which knows nothing
about Slack and is tested without it.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable
from typing import Any

from slack_bolt import App

from incident_copilot.db import repositories as repo
from incident_copilot.db.session import session_scope
from incident_copilot.slack import blocks as B
from incident_copilot.slack import service
from incident_copilot.slack.service import SlackActionRefused

logger = logging.getLogger(__name__)

HELP = (
    "*Incident Copilot*\n"
    "• `/incident` — pick a scenario to simulate (buttons)\n"
    "• `/incident triage <scenario>` — simulate an incident and post an alert\n"
    "• `/incident status <INC-…>` — show an incident's timeline\n"
    "• `/incident list` — list available scenarios\n"
    "Mention me in a thread to ask what happened."
)


def normalise_command_text(raw: str | None) -> str:
    """Clean the argument text of a slash command before parsing it.

    Slack does not always deliver what the user appears to have typed. Composing with code
    formatting active wraps the text in backticks, and smart substitution introduces
    non-breaking spaces and typographic quotes. None of that should change which subcommand
    runs — a user who formatted their message oddly still meant `triage`.

    Without this, the symptom is silent and confusing: the command is accepted, the bot replies,
    and the reply is the help text, with nothing in the logs to say why.
    """
    if not raw:
        return ""
    # NFKC folds most typographic variants back to ASCII equivalents.
    text = unicodedata.normalize("NFKC", raw)
    for junk in ("\u00a0", "\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(junk, " " if junk == "\u00a0" else "")
    text = text.strip().strip("`").strip()
    # A code-formatted command can arrive with the command word repeated in the argument text.
    if text.startswith("/incident"):
        text = text[len("/incident") :].strip()
    return " ".join(text.split())


def delivery_key(body: dict[str, Any], request: Any) -> str:
    """A stable identifier for one inbound delivery.

    Slack sets `X-Slack-Retry-Num` on redeliveries; including it would make a retry look like a
    new delivery, which is exactly backwards. The key is the *interaction*, so a retry collides
    with the original and is refused.
    """
    if event_id := body.get("event_id"):
        return f"event:{event_id}"
    if trigger_id := body.get("trigger_id"):
        return f"trigger:{trigger_id}"
    # Slash commands carry neither; the per-invocation response_url is unique.
    if response_url := body.get("response_url"):
        return f"command:{response_url}"
    return f"fallback:{body.get('token', '')}:{body.get('ts', '')}"


def register(app: App) -> None:
    """Attach every handler to a Bolt app."""

    # -- slash command ----------------------------------------------------

    @app.command("/incident")
    def handle_incident_command(
        ack: Callable[..., None],
        body: dict[str, Any],
        respond: Callable[..., None],
        client: Any,
    ) -> None:
        ack()
        raw = body.get("text")
        text = normalise_command_text(raw)
        channel_id = body["channel_id"]
        user_id = body["user_id"]
        # Logged at INFO because "the bot replied with help and I don't know why" is otherwise
        # undiagnosable from the outside.
        logger.info("/incident from %s: raw=%r parsed=%r", user_id, raw, text)

        parts = text.split()
        # Empty text falls through to the picker, not the help text — see the "" branch.
        verb = parts[0].lower() if parts else ""
        argument = parts[1] if len(parts) > 1 else None

        try:
            if verb == "help":
                respond(text=HELP, response_type="ephemeral")
            elif verb in {"", "list"}:
                # A bare `/incident` lands here rather than on the help text. Slack sometimes
                # delivers the arguments as an empty string, so "no arguments" is at least as
                # likely to mean "the arguments were lost" as "show me the help".
                respond(
                    blocks=B.scenario_picker_blocks(service.scenario_catalogue()),
                    text="Available scenarios",
                    response_type="ephemeral",
                )
            elif verb == "triage":
                if argument is None:
                    respond(
                        blocks=B.scenario_picker_blocks(service.scenario_catalogue()),
                        text="Pick a scenario",
                        response_type="ephemeral",
                    )
                    return
                _triage(client, channel_id, argument, user_id)
            elif verb == "status":
                if argument is None:
                    respond(text="Usage: `/incident status INC-…`", response_type="ephemeral")
                    return
                _status(respond, argument)
            else:
                respond(text=f"Unknown subcommand `{verb}`.\n\n{HELP}", response_type="ephemeral")
        except SlackActionRefused as exc:
            respond(text=f":warning: {exc}", response_type="ephemeral")

    # -- alert card -------------------------------------------------------

    @app.action("investigate_incident")
    def handle_investigate(
        ack: Callable[..., None], body: dict[str, Any], client: Any, request: Any = None
    ) -> None:
        ack()
        user_id = body["user"]["id"]
        channel_id = body["channel"]["id"]
        thread_ts = body["message"]["ts"]
        token = B.read_token(body["actions"][0].get("value"))

        with session_scope() as session:
            if repo.is_duplicate_delivery(session, delivery_key(body, request), "investigate"):
                logger.info("ignoring redelivered investigate interaction")
                return
            try:
                incident = service.start_investigation(session, token or "", actor=user_id)
            except SlackActionRefused as exc:
                _ephemeral(client, channel_id, user_id, f":warning: {exc}")
                return
            service.attach_thread(session, incident, channel_id=channel_id, ts=thread_ts)
            key = incident.key

        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"🔍 <@{user_id}> started an investigation of {key}. "
            "Gathering evidence — I'll post a proposal here.",
        )

    @app.action("triage_scenario")
    def handle_triage_button(
        ack: Callable[..., None], body: dict[str, Any], client: Any, request: Any = None
    ) -> None:
        """Triage from the picker — the path that works when the slash arguments are lost."""
        ack()
        user_id = body["user"]["id"]
        channel_id = body["channel"]["id"]
        scenario_key = str(body["actions"][0].get("value") or "")

        with session_scope() as session:
            if repo.is_duplicate_delivery(session, delivery_key(body, request), "triage"):
                return
        try:
            _triage(client, channel_id, scenario_key, user_id)
        except SlackActionRefused as exc:
            _ephemeral(client, channel_id, user_id, f":warning: {exc}")

    @app.action("ignore_alert")
    def handle_ignore(
        ack: Callable[..., None], body: dict[str, Any], client: Any, request: Any = None
    ) -> None:
        ack()
        user_id = body["user"]["id"]
        channel_id = body["channel"]["id"]
        thread_ts = body["message"]["ts"]
        token = B.read_token(body["actions"][0].get("value"))

        with session_scope() as session:
            if repo.is_duplicate_delivery(session, delivery_key(body, request), "ignore"):
                return
            try:
                incident = service.ignore_alert(session, token or "", actor=user_id)
            except SlackActionRefused as exc:
                _ephemeral(client, channel_id, user_id, f":warning: {exc}")
                return
            key = incident.key

        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"🔕 <@{user_id}> ignored {key}. No investigation started.",
        )

    # -- proposal card ----------------------------------------------------

    def _decide(body: dict[str, Any], client: Any, request: Any) -> None:
        user_id = body["user"]["id"]
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        token = B.read_token(body["actions"][0].get("value"))

        with session_scope() as session:
            if repo.is_duplicate_delivery(session, delivery_key(body, request), "decide"):
                logger.info("ignoring redelivered decision interaction")
                return
            try:
                decision = service.decide(session, token or "", actor=user_id)
            except SlackActionRefused as exc:
                _ephemeral(client, channel_id, user_id, f":warning: {exc}")
                return
            approved = decision.approved
            thread_ts = decision.incident.slack_thread_ts or message_ts
            key = decision.incident.key
            retired = B.decided_blocks(decision.proposal, approved=approved, user_id=user_id)
            fallback = f"{key}: proposal {'approved' if approved else 'rejected'}"

        # Replace the card so its buttons cannot be clicked again. The token already prevents a
        # second redemption; this stops the UI from inviting one.
        client.chat_update(channel=channel_id, ts=message_ts, text=fallback, blocks=retired)
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                f"✅ <@{user_id}> approved the remediation for {key}. Applying it now…"
                if approved
                else f"🚫 <@{user_id}> rejected the proposal for {key}. "
                "No changes made — over to you."
            ),
        )

    @app.action("approve_proposal")
    def handle_approve(
        ack: Callable[..., None], body: dict[str, Any], client: Any, request: Any = None
    ) -> None:
        ack()
        _decide(body, client, request)

    @app.action("reject_proposal")
    def handle_reject(
        ack: Callable[..., None], body: dict[str, Any], client: Any, request: Any = None
    ) -> None:
        ack()
        _decide(body, client, request)

    @app.action("explain_proposal")
    def handle_explain(ack: Callable[..., None], body: dict[str, Any], client: Any) -> None:
        """Open the evidence modal.

        `trigger_id` expires in about three seconds, so the ack and the `views_open` call must
        both happen promptly — this is the one handler where the budget is a hard product
        constraint rather than a politeness.
        """
        ack()
        import json
        import uuid

        raw = body["actions"][0].get("value") or "{}"
        try:
            proposal_id = uuid.UUID(json.loads(raw)["p"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            return

        with session_scope() as session:
            from incident_copilot.db.models import Incident, Proposal

            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                return
            incident = session.get(Incident, proposal.incident_id)
            assert incident is not None
            view = B.explain_modal(incident, proposal, repo.timeline(session, incident))

        client.views_open(trigger_id=body["trigger_id"], view=view)

    # -- mentions ---------------------------------------------------------

    @app.event("app_mention")
    def handle_mention(
        ack: Callable[..., None], body: dict[str, Any], event: dict[str, Any], client: Any
    ) -> None:
        ack()
        channel_id = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        text = event.get("text", "")

        with session_scope() as session:
            incident = None
            for word in text.replace("`", " ").split():
                if word.upper().startswith("INC-"):
                    incident = repo.get_incident_by_key(session, word.upper())
                    break
            if incident is None and event.get("thread_ts"):
                incident = service.incident_for_thread(session, channel_id, event["thread_ts"])

            if incident is None:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="I don't know which incident you mean. "
                    "Mention me in an incident thread, or name it: `@copilot INC-1234`.",
                )
                return

            payload = B.timeline_blocks(incident, repo.timeline(session, incident))
            fallback = f"{incident.key} — {incident.status.value}"

        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=fallback, blocks=payload
        )

    # -- helpers ----------------------------------------------------------

    def _triage(client: Any, channel_id: str, scenario_key: str, user_id: str) -> None:
        with session_scope() as session:
            triaged = service.triage_scenario(
                session, scenario_key, channel_id=channel_id, actor=user_id
            )
            incident = triaged.incident
            payload = B.alert_blocks(
                incident,
                signal=triaged.scenario.alert.signal,
                impact=triaged.scenario.alert.impact,
                token=triaged.investigate_token.token,
            )
            fallback = B.alert_text(incident)
            incident_id = incident.id

        posted = client.chat_postMessage(channel=channel_id, text=fallback, blocks=payload)

        # The thread root is only known after posting, so it is recorded in a second
        # transaction. Until then the incident simply has no thread, which every reader tolerates.
        with session_scope() as session:
            from incident_copilot.db.models import Incident

            stored = session.get(Incident, incident_id)
            if stored is not None:
                service.attach_thread(session, stored, channel_id=channel_id, ts=posted["ts"])

    def _status(respond: Callable[..., None], key: str) -> None:
        with session_scope() as session:
            incident = service.find_incident(session, key)
            payload = B.timeline_blocks(incident, repo.timeline(session, incident))
            fallback = f"{incident.key} — {incident.status.value}"
        respond(blocks=payload, text=fallback, response_type="ephemeral")

    def _ephemeral(client: Any, channel: str, user: str, text: str) -> None:
        """Tell one person something went wrong, without adding noise to the channel."""
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=text)
        except Exception:
            logger.exception("failed to send ephemeral message")
