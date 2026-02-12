import json
from pathlib import Path
from typing import Any, Dict, List, Optional

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _format_recent_changes(changes: dict, limit: int = 3) -> List[str]:
    items = changes.get("recent_changes", []) if isinstance(changes, dict) else []
    out: List[str] = []
    for c in items[:limit]:
        ts = c.get("ts", "?")
        ctype = c.get("type", "?")
        summary = c.get("summary", "")
        out.append(f"{ts} [{ctype}] {summary}".strip())
    return out


def _format_log_highlights(logs: dict, limit: int = 3) -> List[str]:
    lines = logs.get("lines", []) if isinstance(logs, dict) else []
    # pick the most “severe” looking lines first (ERROR > WARN > INFO)
    priority = {"ERROR": 0, "WARN": 1, "INFO": 2}
    def score(line: str) -> int:
        for k, v in priority.items():
            if f" {k} " in line:
                return v
        return 99

    sorted_lines = sorted(lines, key=score)
    return [ln.strip() for ln in sorted_lines[:limit]]


def run_incident_from_fixtures(incident_type: str, alert: Dict[str, Any]) -> Dict[str, Any]:
    """
    Demo-friendly incident runner:
    Loads evidence from fixtures and returns a structured response for Slack posting.
    """
    incident_dir = FIXTURES_DIR / incident_type
    if not incident_dir.exists():
        return {
            "incident_id": f"INC-{alert.get('alert_id','UNKNOWN')}",
            "status": "failed",
            "reason": f"Unknown incident_type '{incident_type}' (no fixtures found).",
        }

    logs = load_fixture(incident_dir / "logs.json")
    metrics = load_fixture(incident_dir / "metrics.json")
    changes = load_fixture(incident_dir / "changes.json")
    runbook = load_fixture(incident_dir / "runbook.json")

    # Pull key metrics safely
    error_rate = metrics.get("error_rate") if isinstance(metrics, dict) else None
    p95_latency_ms = metrics.get("p95_latency_ms") if isinstance(metrics, dict) else None
    upstream_timeout_rate = metrics.get("upstream_timeout_rate") if isinstance(metrics, dict) else None
    req_rps = metrics.get("request_rate_rps") if isinstance(metrics, dict) else None
    window = logs.get("window") if isinstance(logs, dict) else None
    runbook_title = runbook.get("runbook_title") if isinstance(runbook, dict) else None

    recent_changes = _format_recent_changes(changes, limit=3)
    log_highlights = _format_log_highlights(logs, limit=3)
    runbook_steps = runbook.get("steps", []) if isinstance(runbook, dict) else []

    # Choose recommended actions directly from runbook (top few are good)
    recommended_actions = runbook_steps[:4] if isinstance(runbook_steps, list) else []
    mitigations = [s for s in runbook_steps if isinstance(s, str) and s.lower().startswith("mitigation")]
    top_mitigations = mitigations[:3]

    return {
        "incident_id": f"INC-{alert.get('alert_id','UNKNOWN')}",
        "status": "in_progress",
        "severity": alert.get("severity", "SEV2"),
        "service": alert.get("service", "unknown"),
        "summary": alert.get("short_summary", ""),
        "evidence": {
            "metrics_window": metrics.get("time_range") if isinstance(metrics, dict) else None,
            "error_rate": error_rate,
            "p95_latency_ms": p95_latency_ms,
            "upstream_timeout_rate": upstream_timeout_rate,
            "request_rate_rps": req_rps,
            "log_window": window,
            "log_highlights": log_highlights,
            "recent_changes": recent_changes,
            "runbook_title": runbook_title,
        },
        "recommended_actions": recommended_actions,
        "suggested_mitigations": top_mitigations,
        "next_update_minutes": 15,
    }
