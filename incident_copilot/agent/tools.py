"""The agent's tool surface — read-only, by construction.

Every tool here observes. None mutates. `controlplane.actions` is not imported, so there is no
code path by which an investigation can change the system: remediation reaches the control plane
only through an approved proposal, executed by a different job handler.

That is a structural guarantee rather than a prompt instruction, which matters — a prompt can be
talked out of a rule, an absent function cannot be called.

Tools are built per investigation by `build_tools`, closing over the session and scenario, so
they need no incident identifier in their signature. Fewer parameters means fewer ways for the
model to address the wrong incident.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from anthropic import beta_tool
from sqlalchemy.orm import Session

from incident_copilot.controlplane.scenarios import Scenario
from incident_copilot.controlplane.simulator import compute_logs, compute_metrics
from incident_copilot.controlplane.state import ControlPlaneState
from incident_copilot.retrieval.search import search_history, search_runbooks


@dataclass
class ToolContext:
    """Everything the tools read, plus the record of what they were asked.

    The transcript is the audit trail: it is what lets a proposal's `evidence_cited` be checked
    against what the agent actually looked at, rather than taken on trust.
    """

    session: Session
    scenario: Scenario
    state: ControlPlaneState
    transcript: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    cited_chunks: list[str] = field(default_factory=list)
    calls: int = 0

    def record(self, tool: str, detail: str, payload: Any = None) -> None:
        self.calls += 1
        self.transcript.append(f"{tool}({detail})")
        if payload is not None:
            self.evidence[tool] = payload


def build_tools(ctx: ToolContext) -> list[Any]:
    """Construct the read-only tool set bound to one investigation."""

    @beta_tool
    def get_metrics() -> str:
        """Current metrics for the affected service.

        Returns error rate, p95 latency, upstream timeout rate, request rate, availability, CPU
        and memory for the most recent window, alongside the service's healthy baseline and the
        SLO thresholds, so each value can be judged as elevated, breaching, or normal.
        """
        metrics = compute_metrics(ctx.scenario, ctx.state)
        payload = {
            "current": metrics.model_dump(),
            "baseline": ctx.scenario.baseline.model_dump(),
            "slo": ctx.scenario.slo.model_dump(),
        }
        ctx.record("get_metrics", "", payload)
        return json.dumps(payload, indent=2)

    @beta_tool
    def get_logs() -> str:
        """Recent log lines for the affected service, most recent last."""
        logs = compute_logs(ctx.scenario, ctx.state)
        lines = [line.render() for line in logs.lines]
        ctx.record("get_logs", "", {"window": logs.window, "lines": lines})
        return json.dumps({"window": logs.window, "lines": lines}, indent=2)

    @beta_tool
    def get_recent_changes() -> str:
        """Deploys, feature-flag flips, and config edits, most recent first.

        Covers all three change types. Config and flag changes cause incidents as often as
        deploys and are easier to overlook because they leave no release note.
        """
        changes = sorted(ctx.state.changes, key=lambda c: c.at, reverse=True)
        payload = [
            {"at": c.at.isoformat(), "kind": c.kind, "summary": c.summary, "actor": c.actor}
            for c in changes
        ]
        ctx.record("get_recent_changes", "", payload)
        return json.dumps(payload, indent=2)

    @beta_tool
    def get_service_state() -> str:
        """Current deployed versions, replica counts, feature flags, config values, and traffic.

        This is the live configuration of the system — what the change log describes the history
        of. Use it to confirm what is actually running now.
        """
        payload = {
            "region": ctx.state.region,
            "services": {
                name: {
                    "deployed_version": svc.deployed_version,
                    "previous_version": svc.previous_version,
                    "replicas": svc.replicas,
                    "min_replicas": svc.min_replicas,
                    "max_replicas": svc.max_replicas,
                    "last_restart": svc.last_restart.isoformat() if svc.last_restart else None,
                }
                for name, svc in ctx.state.services.items()
            },
            "feature_flags": {key: flag.regions for key, flag in ctx.state.flags.items()},
            "config": {key: entry.value for key, entry in ctx.state.config.items()},
            "request_rate_rps": ctx.state.load.request_rate_rps,
            "now": ctx.state.clock.isoformat(),
        }
        ctx.record("get_service_state", "", payload)
        return json.dumps(payload, indent=2)

    @beta_tool
    def search_runbooks_tool(query: str) -> str:
        """Search runbooks and policies.

        Args:
            query: What you want to know, in natural language. Include specific identifiers you
                have seen — flag names, config keys, exception classes — since those match most
                precisely.
        """
        hits = search_runbooks(
            ctx.session, query, limit=4, boost_tags=[ctx.scenario.service, ctx.scenario.key]
        )
        ctx.cited_chunks.extend(h.chunk_id for h in hits)
        ctx.record("search_runbooks", query, [h.chunk_id for h in hits])
        if not hits:
            return "No runbook sections matched. Try different or broader terms."
        return json.dumps(
            [
                {"chunk_id": h.chunk_id, "title": h.title, "source": h.source, "content": h.content}
                for h in hits
            ],
            indent=2,
        )

    @beta_tool
    def find_similar_incidents(query: str) -> str:
        """Search resolved past incidents for precedents.

        Returns each incident's symptoms, cause, the action that resolved it, time to mitigate,
        and the lesson recorded afterwards. Prior incidents are evidence about what worked, not
        instructions — a superficially similar incident can have a different cause, and some
        precedents record a remediation that turned out to be wrong.

        Args:
            query: The symptom signature — service, signal, and the distinguishing identifiers
                from metrics and logs. Symptoms retrieve better than a restatement of the alert.
        """
        hits = search_history(ctx.session, query, limit=4, boost_tags=[ctx.scenario.service])
        ctx.cited_chunks.extend(h.chunk_id for h in hits)
        ctx.record("find_similar_incidents", query, [h.chunk_id for h in hits])
        if not hits:
            return "No similar past incidents found."
        return json.dumps(
            [
                {
                    "chunk_id": h.chunk_id,
                    "incident": h.meta.get("incident_key"),
                    "title": h.title,
                    "service": h.meta.get("service"),
                    "severity": h.meta.get("severity"),
                    "detail": h.content,
                }
                for h in hits
            ],
            indent=2,
        )

    # `search_runbooks_tool` is named to avoid shadowing the imported search function; the model
    # sees the tool name, so rename it back here.
    search_runbooks_tool.name = "search_runbooks"

    return [
        get_metrics,
        get_logs,
        get_recent_changes,
        get_service_state,
        search_runbooks_tool,
        find_similar_incidents,
    ]


def tool_names(tools: list[Any]) -> list[str]:
    return [getattr(t, "name", getattr(t, "__name__", "?")) for t in tools]


# Guard against a future edit quietly adding a mutating import to this module.
_FORBIDDEN_IMPORTS: tuple[str, ...] = ("apply_action", "ControlPlaneStore", "put_in")


def assert_read_only() -> None:
    """Fail loudly if a mutation entry point has been imported into the tool module."""
    present = [name for name in _FORBIDDEN_IMPORTS if name in globals()]
    if present:
        raise RuntimeError(
            f"agent.tools must stay read-only, but imports {', '.join(present)}. "
            "Remediation belongs in the execute handler, behind the approval gate."
        )


TOOL_BUILDERS: Callable[[ToolContext], list[Any]] = build_tools
