"""Postgres-backed control-plane store.

Implements the `ControlPlaneStore` protocol from phase 1, so the simulator, actions, and API
routes are unchanged by the swap.

State is written under optimistic concurrency. Two workers remediating the same incident — which
at-least-once delivery makes possible — would otherwise read the same state, apply different
actions, and have the second write silently discard the first. Here the loser gets
`ConcurrentStateChange` and can re-read and retry.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from incident_copilot.controlplane.scenarios import ScenarioRegistry, get_registry
from incident_copilot.controlplane.state import ControlPlaneState
from incident_copilot.db.models import ControlPlaneStateRow
from incident_copilot.db.session import session_scope


class ConcurrentStateChange(Exception):
    """The state changed underneath a writer; re-read and retry."""


def _serialise(state: ControlPlaneState) -> dict[str, Any]:
    return state.model_dump(mode="json")


class PostgresControlPlaneStore:
    """Durable, multi-replica control-plane state."""

    def __init__(self, registry: ScenarioRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    # -- protocol ----------------------------------------------------------

    def get(self, scenario_key: str) -> ControlPlaneState:
        with session_scope() as session:
            return self.get_in(session, scenario_key)

    def put(self, state: ControlPlaneState) -> None:
        with session_scope() as session:
            self.put_in(session, state)

    def reset(self, scenario_key: str) -> ControlPlaneState:
        with session_scope() as session:
            return self.reset_in(session, scenario_key)

    def reset_all(self) -> None:
        with session_scope() as session:
            session.query(ControlPlaneStateRow).delete()

    # -- session-scoped variants -------------------------------------------
    #
    # Job handlers already own a session and a transaction. Reusing it keeps a remediation and
    # the event it records in one atomic unit, instead of leaving a timeline entry that claims
    # an action happened when its state write rolled back.

    def get_in(self, session: Session, scenario_key: str) -> ControlPlaneState:
        row = session.get(ControlPlaneStateRow, scenario_key)
        if row is None:
            return self._initialise(session, scenario_key)
        return ControlPlaneState.model_validate(row.state)

    def version_in(self, session: Session, scenario_key: str) -> int:
        row = session.get(ControlPlaneStateRow, scenario_key)
        return row.version if row is not None else 0

    def put_in(
        self, session: Session, state: ControlPlaneState, *, expected_version: int | None = None
    ) -> int:
        """Write state, returning the new version.

        With `expected_version`, the write is conditional and raises `ConcurrentStateChange` if
        another writer got there first. Without it, the write is last-writer-wins — acceptable
        for the operator/debug routes, not for remediation.
        """
        payload = _serialise(state)

        if expected_version is None:
            stmt = (
                pg_insert(ControlPlaneStateRow)
                .values(scenario_key=state.scenario, state=payload, version=1)
                .on_conflict_do_update(
                    index_elements=["scenario_key"],
                    set_={
                        "state": payload,
                        "version": ControlPlaneStateRow.__table__.c.version + 1,
                    },
                )
                .returning(ControlPlaneStateRow.version)
            )
            version = session.execute(stmt).scalar_one()
            session.flush()
            return int(version)

        result = session.execute(
            update(ControlPlaneStateRow)
            .where(
                ControlPlaneStateRow.scenario_key == state.scenario,
                ControlPlaneStateRow.version == expected_version,
            )
            .values(state=payload, version=expected_version + 1)
            .returning(ControlPlaneStateRow.version)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ConcurrentStateChange(
                f"control-plane state for '{state.scenario}' changed since version "
                f"{expected_version}; re-read before writing"
            )
        session.flush()
        return int(row)

    def reset_in(self, session: Session, scenario_key: str) -> ControlPlaneState:
        state = self._registry.get(scenario_key).initial_state()
        self.put_in(session, state)
        return state

    # -- internals ---------------------------------------------------------

    def _initialise(self, session: Session, scenario_key: str) -> ControlPlaneState:
        """Materialise a scenario's starting state on first access.

        Two workers can reach this concurrently on the first touch of a scenario, so the insert
        tolerates a conflict and re-reads rather than failing.
        """
        state = self._registry.get(scenario_key).initial_state()
        session.execute(
            pg_insert(ControlPlaneStateRow)
            .values(scenario_key=scenario_key, state=_serialise(state), version=1)
            .on_conflict_do_nothing(index_elements=["scenario_key"])
        )
        session.flush()
        stored = session.execute(
            select(ControlPlaneStateRow.state).where(
                ControlPlaneStateRow.scenario_key == scenario_key
            )
        ).scalar_one()
        return ControlPlaneState.model_validate(stored)
