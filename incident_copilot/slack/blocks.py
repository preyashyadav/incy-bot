"""Block Kit builders.

Two constraints shape everything here.

**Button values carry only an opaque token.** Whatever goes into a button's `value` comes back
from a browser and is entirely client-controlled — it is a request parameter, not state. The
previous version of this project put the whole alert payload in there and fed it straight into
the workflow. Here a button carries a token and nothing else; the decision it authorises is
looked up server-side.

**Every message has a `text` fallback.** It is what appears in notifications, in the sidebar
preview, and to screen readers. A blocks-only message shows up as an empty notification.
"""

from __future__ import annotations

import json
from typing import Any

from incident_copilot.db.models import ApprovalToken, Incident, IncidentEvent, Proposal

Block = dict[str, Any]

SEVERITY_EMOJI = {"SEV1": "🔴", "SEV2": "🟠", "SEV3": "🟡"}
RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}
CONFIDENCE_EMOJI = {"high": "◆◆◆", "medium": "◆◆◇", "low": "◆◇◇"}

ACTION_LABELS = {
    "rollback_deploy": "Roll back deploy",
    "toggle_feature_flag": "Toggle feature flag",
    "set_config_value": "Change config",
    "scale_replicas": "Scale replicas",
    "restart_service": "Restart service",
    "no_action": "No action",
}

# Slack truncates section text at 3000 characters and rejects the message outright beyond it.
_SECTION_LIMIT = 2900


def _mrkdwn(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _clip(text)}}


def _clip(text: str, limit: int = _SECTION_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(text, 400)}]}


def _bullets(items: list[str], empty: str = "_none_") -> str:
    return "\n".join(f"• {item}" for item in items) if items else empty


def token_value(token: str) -> str:
    """A button payload. Deliberately just the token — see the module docstring."""
    return json.dumps({"t": token})


def read_token(value: str | None) -> str | None:
    """Extract a token from a button payload, tolerating anything malformed.

    This parses untrusted input, so it never raises: a forged or corrupted value simply yields
    no token and is refused downstream like any unknown token.
    """
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    token = parsed.get("t")
    return token if isinstance(token, str) else None


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------


def alert_text(incident: Incident) -> str:
    return f"{SEVERITY_EMOJI.get(incident.severity, '⚪')} {incident.severity} {incident.title}"


def alert_blocks(incident: Incident, *, signal: str, impact: str, token: str) -> list[Block]:
    """The card an alert arrives as. One button starts an investigation; nothing else acts."""
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚨 {incident.title}"[:150], "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident*\n`{incident.key}`"},
                {
                    "type": "mrkdwn",
                    "text": f"*Severity*\n{SEVERITY_EMOJI.get(incident.severity, '⚪')} "
                    f"{incident.severity}",
                },
                {"type": "mrkdwn", "text": f"*Service*\n{incident.service}"},
                {"type": "mrkdwn", "text": f"*Region*\n{incident.region or '—'}"},
                {"type": "mrkdwn", "text": f"*Signal*\n`{signal}`"},
                {"type": "mrkdwn", "text": f"*Impact*\n{impact}"},
            ],
        },
        {
            "type": "actions",
            "block_id": "alert_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "investigate_incident",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Investigate", "emoji": True},
                    "value": token_value(token),
                },
                {
                    "type": "button",
                    "action_id": "ignore_alert",
                    "text": {"type": "plain_text", "text": "Ignore", "emoji": True},
                    "value": token_value(token),
                },
            ],
        },
        _context("The copilot only reads while investigating. Any change needs your approval."),
    ]


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


def describe_action(action: dict[str, Any]) -> str:
    """One line describing a proposed action, without needing the control plane."""
    kind = action.get("kind", "?")
    match kind:
        case "rollback_deploy":
            return f"Roll back *{action.get('service')}* to its previous version"
        case "toggle_feature_flag":
            verb = "Enable" if action.get("enabled") else "Disable"
            where = action.get("region") or "the affected region"
            return f"{verb} `{action.get('flag')}` in {where}"
        case "set_config_value":
            return f"Set `{action.get('key')}` to `{action.get('value')}`"
        case "scale_replicas":
            return f"Scale *{action.get('service')}* to {action.get('replicas')} replicas"
        case "restart_service":
            return f"Restart *{action.get('service')}*"
        case "no_action":
            return "Take no action"
        case _:
            return str(kind)


def proposal_text(incident: Incident, proposal: Proposal) -> str:
    return f"{incident.key}: proposed remediation awaiting approval — {proposal.hypothesis[:120]}"


def proposal_blocks(
    incident: Incident,
    proposal: Proposal,
    *,
    approve_token: ApprovalToken,
    reject_token: ApprovalToken,
) -> list[Block]:
    """The approval card. This is the human decision point of the whole system."""
    raw = proposal.raw or {}
    is_no_action = len(proposal.actions) == 1 and proposal.actions[0].get("kind") == "no_action"

    blocks: list[Block] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ("✅ No action needed" if is_no_action else "🔧 Remediation proposed"),
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident*\n`{incident.key}`"},
                {
                    "type": "mrkdwn",
                    "text": f"*Severity*\n{SEVERITY_EMOJI.get(proposal.severity, '⚪')} "
                    f"{proposal.severity}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Confidence*\n{CONFIDENCE_EMOJI.get(proposal.confidence, '')} "
                    f"{proposal.confidence}",
                },
                {"type": "mrkdwn", "text": f"*Service*\n{incident.service}"},
            ],
        },
        _mrkdwn(f"*What I think is happening*\n{proposal.hypothesis}"),
    ]

    if ruled_out := raw.get("ruled_out"):
        blocks.append(_mrkdwn(f"*Ruled out*\n{_bullets(list(ruled_out))}"))

    blocks.append({"type": "divider"})

    if is_no_action:
        reason = proposal.actions[0].get("reason") or proposal.actions[0].get("rationale", "")
        blocks.append(_mrkdwn(f"*Recommendation*\nTake no action. {reason}"))
    else:
        lines = []
        for index, action in enumerate(proposal.actions, start=1):
            risk = str(action.get("risk", "medium"))
            reversible = "reversible" if action.get("reversible") else "*not reversible*"
            lines.append(
                f"*{index}. {describe_action(action)}*\n"
                f"{RISK_EMOJI.get(risk, '⚪')} {risk} risk · {reversible}\n"
                f"_{action.get('rationale', '')}_"
            )
        blocks.append(_mrkdwn("*Proposed actions*\n\n" + "\n\n".join(lines)))

    if proposal.verification_plan:
        blocks.append(_mrkdwn(f"*How we'll know it worked*\n{proposal.verification_plan}"))

    if raw.get("requires_followup"):
        note = raw.get("followup_note") or "This mitigates without fixing the underlying cause."
        blocks.append(_mrkdwn(f"⚠️ *Mitigation only* — {note}"))

    footer: list[str] = []
    if proposal.similar_incidents:
        footer.append("Prior incidents: " + ", ".join(f"`{k}`" for k in proposal.similar_incidents))
    if proposal.evidence_cited:
        footer.append(f"{len(proposal.evidence_cited)} evidence source(s) cited")
    if footer:
        blocks.append(_context(" · ".join(footer)))

    approve_label = "Confirm no action" if is_no_action else "Approve & apply"
    blocks.append(
        {
            "type": "actions",
            "block_id": "proposal_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve_proposal",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": approve_label, "emoji": True},
                    "value": token_value(approve_token.token),
                    # A confirmation dialog on an irreversible-looking action is cheap insurance
                    # against a misclick in a busy incident channel.
                    **(
                        {}
                        if is_no_action
                        else {
                            "confirm": {
                                "title": {"type": "plain_text", "text": "Apply this remediation?"},
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "This will change the running system:\n"
                                    + _bullets([describe_action(a) for a in proposal.actions]),
                                },
                                "confirm": {"type": "plain_text", "text": "Apply"},
                                "deny": {"type": "plain_text", "text": "Cancel"},
                            }
                        }
                    ),
                },
                {
                    "type": "button",
                    "action_id": "reject_proposal",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "value": token_value(reject_token.token),
                },
                {
                    "type": "button",
                    "action_id": "explain_proposal",
                    "text": {"type": "plain_text", "text": "Show evidence", "emoji": True},
                    "value": json.dumps({"p": str(proposal.id)}),
                },
            ],
        }
    )
    return blocks


def decided_blocks(proposal: Proposal, *, approved: bool, user_id: str) -> list[Block]:
    """Replaces the proposal card once someone decides, so the buttons cannot be clicked again."""
    verdict = "approved" if approved else "rejected"
    icon = "✅" if approved else "🚫"
    return [
        _mrkdwn(f"{icon} *Proposal {verdict}* by <@{user_id}>\n_{proposal.hypothesis}_"),
        _context(f"Decided at {proposal.decided_at:%H:%M UTC}" if proposal.decided_at else ""),
    ]


# ---------------------------------------------------------------------------
# Evidence modal
# ---------------------------------------------------------------------------


def explain_modal(incident: Incident, proposal: Proposal, events: list[IncidentEvent]) -> Block:
    """The audit trail, on demand.

    This is what turns a proposal from an assertion into something checkable: which tools ran,
    which documents were retrieved, and what the model claims each conclusion rests on.
    """
    evidence_event = next(
        (e for e in reversed(events) if e.type.value == "evidence_gathered"), None
    )
    payload = evidence_event.payload if evidence_event else {}

    blocks: list[Block] = [
        _mrkdwn(f"*{incident.key}* — {incident.title}"),
        _mrkdwn(f"*Hypothesis*\n{proposal.hypothesis}"),
    ]

    if transcript := payload.get("transcript"):
        blocks.append(_mrkdwn("*Tools called*\n" + _bullets([f"`{t}`" for t in transcript])))
    if chunks := payload.get("cited_chunks"):
        blocks.append(
            _mrkdwn("*Documents retrieved*\n" + _bullets([f"`{c}`" for c in chunks[:12]]))
        )
    if proposal.evidence_cited:
        blocks.append(
            _mrkdwn("*Cited as support*\n" + _bullets([f"`{c}`" for c in proposal.evidence_cited]))
        )
    if proposal.similar_incidents:
        blocks.append(
            _mrkdwn(
                "*Prior incidents*\n" + _bullets([f"`{k}`" for k in proposal.similar_incidents])
            )
        )
    if payload.get("input_tokens"):
        blocks.append(
            _context(
                f"{payload.get('tool_calls', 0)} tool calls · "
                f"{payload.get('input_tokens', 0):,} in / "
                f"{payload.get('output_tokens', 0):,} out tokens"
            )
        )

    return {
        "type": "modal",
        "callback_id": "evidence_modal",
        "title": {"type": "plain_text", "text": "Evidence"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

_EVENT_ICONS = {
    "created": "📥",
    "investigation_started": "🔍",
    "evidence_gathered": "📊",
    "proposal_created": "💡",
    "approved": "✅",
    "rejected": "🚫",
    "action_executed": "🔧",
    "action_failed": "❌",
    "verified": "🎉",
    "verification_failed": "⚠️",
    "resolved": "🏁",
    "note_added": "📝",
    "error": "💥",
}


def timeline_blocks(incident: Incident, events: list[IncidentEvent]) -> list[Block]:
    lines = []
    for event in events:
        icon = _EVENT_ICONS.get(event.type.value, "•")
        actor = f" · <@{event.actor}>" if event.actor.startswith("U") else ""
        detail = ""
        if event.type.value == "evidence_gathered":
            detail = f" ({event.payload.get('tool_calls', 0)} tools)"
        elif event.type.value == "note_added" and event.payload.get("note"):
            detail = f" ({event.payload['from']} → {event.payload['to']})"
        lines.append(
            f"{icon} `{event.created_at:%H:%M}` {event.type.value.replace('_', ' ')}{detail}{actor}"
        )

    return [
        _mrkdwn(
            f"*{incident.key}* — {incident.title}\n"
            f"{SEVERITY_EMOJI.get(incident.severity, '⚪')} {incident.severity} · "
            f"status *{incident.status.value.replace('_', ' ')}*"
        ),
        _mrkdwn("*Timeline*\n" + "\n".join(lines)),
    ]


def scenario_picker_blocks(scenarios: list[tuple[str, str, str]]) -> list[Block]:
    """The scenario menu, with a button per scenario.

    Buttons rather than instructions-to-retype, because Slack does not reliably deliver a slash
    command's argument text: composing the command with rich-text formatting active sends the
    command with an empty `text`, and the user sees a help message with no explanation. A button
    cannot lose its payload that way.

    The value here is a plain scenario key rather than a signed token, unlike the approval
    buttons. That is deliberate and not an inconsistency: triage creates a demo incident and
    resets a simulated scenario, which anyone able to run the slash command can already do. The
    key is validated against the registry, and an unknown one is refused. Tokens guard changes
    to the running system; this changes nothing.
    """
    blocks: list[Block] = [_mrkdwn("*Which incident should I simulate?*")]
    for key, title, severity in scenarios:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{SEVERITY_EMOJI.get(severity, '⚪')} *{title}*\n`{key}`",
                },
                "accessory": {
                    "type": "button",
                    "action_id": "triage_scenario",
                    "text": {"type": "plain_text", "text": "Triage", "emoji": True},
                    "value": key,
                },
            }
        )
    blocks.append(
        _context(
            "Or type `/incident triage <scenario>` — as *plain text*, not inside a code block."
        )
    )
    return blocks
