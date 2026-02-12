from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import requests

from app.agent_tools import TOOL_SCHEMAS, dispatch_tool

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))


SYSTEM_PROMPT = (
    "You are the incident response brain for a demo service. "
    "Use tools to create the incident, fetch evidence, and consult the KB. "
    "Then return ONLY a JSON object with the exact fields:\n"
    "{\n"
    '  "incident_id": string,\n'
    '  "status": "in_progress" | "failed",\n'
    '  "severity": "SEV1" | "SEV2" | "SEV3",\n'
    '  "service": string,\n'
    '  "summary": string,\n'
    '  "evidence": {\n'
    '    "metrics_window": string | null,\n'
    '    "error_rate": number | null,\n'
    '    "p95_latency_ms": number | null,\n'
    '    "upstream_timeout_rate": number | null,\n'
    '    "request_rate_rps": number | null,\n'
    '    "log_window": string | null,\n'
    '    "log_highlights": string[],\n'
    '    "recent_changes": string[],\n'
    '    "runbook_title": string | null\n'
    "  },\n"
    '  "recommended_actions": string[],\n'
    '  "suggested_mitigations": string[],\n'
    '  "next_update_minutes": number\n'
    "}\n"
    "Do not include extra keys or text."
)


def _openai_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_output_text(output_items: List[Dict[str, Any]]) -> str:
    chunks: List[str] = []
    for item in output_items:
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if part.get("type") in ("output_text", "text"):
                    text = part.get("text")
                    if text:
                        chunks.append(text)
    return "\n".join(chunks).strip()


def _parse_tool_args(arg_value: Any) -> Dict[str, Any]:
    if isinstance(arg_value, dict):
        return arg_value
    if isinstance(arg_value, str):
        try:
            return json.loads(arg_value)
        except json.JSONDecodeError:
            return {}
    return {}


def _openai_response(input_items: List[Dict[str, Any]], api_key: str) -> Dict[str, Any]:
    payload = {
        "model": OPENAI_MODEL,
        "input": input_items,
        "tools": TOOL_SCHEMAS,
    }
    resp = requests.post(
        f"{OPENAI_BASE_URL}/responses",
        headers=_openai_headers(api_key),
        json=payload,
        timeout=OPENAI_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def run_incident_agent(alert: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"status": "failed", "reason": "openai_not_configured"}

    input_items: List[Dict[str, Any]] = [
        {"type": "message", "role": "system", "content": SYSTEM_PROMPT},
        {"type": "message", "role": "user", "content": json.dumps(alert)},
    ]

    for _ in range(6):
        response = _openai_response(input_items, api_key=api_key)
        output_items = response.get("output", [])
        if not isinstance(output_items, list):
            return {"status": "failed", "reason": "openai_bad_output"}

        input_items.extend(output_items)

        tool_calls = [item for item in output_items if item.get("type") == "function_call"]
        if tool_calls:
            for call in tool_calls:
                name = call.get("name")
                call_id = call.get("call_id")
                args = _parse_tool_args(call.get("arguments"))
                result = dispatch_tool(name, args) if name else {"ok": False, "reason": "missing_tool_name"}
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    }
                )
            continue

        output_text = _extract_output_text(output_items) or response.get("output_text", "")
        if not output_text:
            return {"status": "failed", "reason": "openai_empty_output"}

        try:
            return json.loads(output_text)
        except json.JSONDecodeError:
            return {"status": "failed", "reason": "openai_non_json", "raw": output_text}

    return {"status": "failed", "reason": "openai_max_steps"}
