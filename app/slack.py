import os
import json
import hmac
import hashlib
import threading
from typing import Any, Dict

import requests
from fastapi import APIRouter, Request, Header, HTTPException

# OpenAI agent
from app.agent import run_incident_agent
# fixture-driven demo
from app.incident_runner import run_incident_from_fixtures

router = APIRouter()
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(status_code=500, detail=f"Missing env var: {name}")
    return value


def verify_slack_signature(body: bytes, timestamp: str, signature: str):
    signing_secret = get_env("SLACK_SIGNING_SECRET")
    basestring = f"v0:{timestamp}:{body.decode()}".encode()
    my_signature = "v0=" + hmac.new(
        signing_secret.encode(), basestring, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(my_signature, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


def slack_api_post(method: str, payload: dict) -> dict:
    token = get_env("SLACK_BOT_TOKEN")
    url = f"https://slack.com/api/{method}"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=10,
    )
    return resp.json()


def _format_incident_text(result: Dict[str, Any]) -> str:
    evidence = result.get("evidence", {}) or {}
    rec_actions = result.get("recommended_actions", []) or []
    mitigations = result.get("suggested_mitigations", []) or []

    recent_changes = evidence.get("recent_changes", []) or []
    log_highlights = evidence.get("log_highlights", []) or []

    def pct(x):
        try:
            return f"{float(x) * 100:.1f}%"
        except Exception:
            return str(x)

    text = (
        f"*🧩 Incident Started:* `{result.get('incident_id')}`\n"
        f"*Service:* {result.get('service')}\n"
        f"*Severity:* {result.get('severity')}\n"
        f"*Summary:* {result.get('summary')}\n\n"
        f"*📈 Evidence (Metrics)*\n"
        f"• Window: {evidence.get('metrics_window')}\n"
        f"• Error rate: {pct(evidence.get('error_rate'))}\n"
        f"• P95 latency (ms): {evidence.get('p95_latency_ms')}\n"
        f"• Upstream timeout rate: {pct(evidence.get('upstream_timeout_rate'))}\n"
        f"• Request rate (rps): {evidence.get('request_rate_rps')}\n"
        f"• Runbook: {evidence.get('runbook_title')}\n\n"
        f"*🧾 Recent Changes*\n"
        + ("• " + "\n• ".join(recent_changes) if recent_changes else "• (none)") + "\n\n"
        f"*📜 Log Highlights ({evidence.get('log_window')})*\n"
        + ("• " + "\n• ".join(log_highlights) if log_highlights else "• (none)") + "\n\n"
        f"*✅ Recommended Actions*\n"
        + ("• " + "\n• ".join(rec_actions) if rec_actions else "• (none)") + "\n\n"
        f"*🛠️ Suggested Mitigations*\n"
        + ("• " + "\n• ".join(mitigations) if mitigations else "• (none)") + "\n\n"
        f"Next update in *{result.get('next_update_minutes', 15)} minutes*. 🔁"
    )
    return text


def _run_backend_engine_and_post(
    *,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    alert_id: str,
    alert_data: dict,
):
    try:
        result = run_incident_agent(alert_data)
        if result.get("status") == "failed" and result.get("reason") == "openai_not_configured":
            result = run_incident_from_fixtures("payments_failing", alert_data)

        if result.get("status") == "failed":
            slack_api_post("chat.postMessage", {
                "channel": channel_id,
                "thread_ts": thread_ts,
                "text": f"❌ Incident workflow failed for `{alert_id}`: {result.get('reason', 'unknown')} 🛑"
            })
            return

        slack_api_post("chat.postMessage", {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "text": _format_incident_text(result) + " ✅"
        })

    except Exception as e:
        slack_api_post("chat.postMessage", {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "text": f"❌ Unexpected error while running incident for `{alert_id}`: {type(e).__name__} 🛑"
        })


@router.post("/slack/alert")
async def post_alert_to_slack(payload: dict):
    token = get_env("SLACK_BOT_TOKEN")
    channel = get_env("SLACK_CHANNEL_ID")

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*🚨 Incident Alert Detected*\n\n"
                    f"*Service:* {payload['service']}\n"
                    f"*Severity:* {payload['severity']}\n"
                    f"*Summary:* {payload['short_summary']}\n"
                    f"*Time:* {payload['timestamp']}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Initiate Incident Response"},
                    "style": "primary",
                    "value": json.dumps(payload),
                    "action_id": "initiate_incident",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Ignore Alert"},
                    "style": "danger",
                    "value": json.dumps(payload),
                    "action_id": "ignore_alert",
                },
            ],
        },
    ]

    resp = requests.post(
        SLACK_POST_MESSAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "channel": channel,
            "blocks": blocks,
            "text": "Incident alert",
        },
        timeout=10,
    )

    return {"status": "sent", "slack_response": resp.json()}


@router.post("/slack/actions")
async def handle_slack_actions(
    request: Request,
    x_slack_signature: str = Header(None),
    x_slack_request_timestamp: str = Header(None),
):
    body = await request.body()
    verify_slack_signature(body, x_slack_request_timestamp, x_slack_signature)

    form = await request.form()
    payload = json.loads(form["payload"])

    action = payload["actions"][0]
    action_id = action["action_id"]
    alert_data = json.loads(action["value"])

    channel_id = payload["channel"]["id"]
    thread_ts = payload["message"]["ts"]
    user_id = payload["user"]["id"]

    alert_id = alert_data.get("alert_id", "unknown")

    if action_id == "ignore_alert":
        slack_api_post("chat.postMessage", {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "text": f" <@{user_id}> ignored alert `{alert_id}`. No incident started. 🧾"
        })
        return {}

    if action_id == "initiate_incident":
        slack_api_post("chat.postMessage", {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "text": f"🚀 <@{user_id}> approved `{alert_id}`. Running backend workflow now… ⏳"
        })

        t = threading.Thread(
            target=_run_backend_engine_and_post,
            kwargs={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "user_id": user_id,
                "alert_id": alert_id,
                "alert_data": alert_data,
            },
            daemon=True,
        )
        t.start()
        return {}

    slack_api_post("chat.postMessage", {
        "channel": channel_id,
        "thread_ts": thread_ts,
        "text": f"⚠️ Unknown action `{action_id}` received. 🤔"
    })
    return {}
