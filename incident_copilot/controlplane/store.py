"""Where live control-plane state lives between reads.

Phase 1 keeps state in memory. Phase 2 swaps in a Postgres-backed implementation behind the
same `ControlPlaneStore` protocol — the simulator, actions, and API routes are written against
the protocol and will not change.
"""

from __future__ import annotations

import threading
from typing import Protocol

from incident_copilot.controlplane.scenarios import ScenarioRegistry, get_registry
from incident_copilot.controlplane.state import ControlPlaneState


class ControlPlaneStore(Protocol):
    """Read/write access to the live state of one scenario instance."""

    def get(self, scenario_key: str) -> ControlPlaneState:
        """Current state, initialising from the scenario pack on first access."""
        ...

    def put(self, state: ControlPlaneState) -> None: ...

    def reset(self, scenario_key: str) -> ControlPlaneState:
        """Discard live state and return to the scenario's starting point."""
        ...

    def reset_all(self) -> None: ...


class InMemoryControlPlaneStore:
    """Process-local store.

    Guarded by a lock because the API and (from phase 2) the worker both touch it, and
    read-modify-write of a whole state object is not atomic. Process-local means it does not
    survive a restart and does not span replicas — both acceptable in phase 1, and both fixed
    by the Postgres implementation.
    """

    def __init__(self, registry: ScenarioRegistry | None = None) -> None:
        self._registry = registry or get_registry()
        self._states: dict[str, ControlPlaneState] = {}
        self._lock = threading.Lock()

    def get(self, scenario_key: str) -> ControlPlaneState:
        with self._lock:
            state = self._states.get(scenario_key)
            if state is None:
                state = self._registry.get(scenario_key).initial_state()
                self._states[scenario_key] = state
            return state

    def put(self, state: ControlPlaneState) -> None:
        with self._lock:
            self._states[state.scenario] = state

    def reset(self, scenario_key: str) -> ControlPlaneState:
        state = self._registry.get(scenario_key).initial_state()
        with self._lock:
            self._states[scenario_key] = state
        return state

    def reset_all(self) -> None:
        with self._lock:
            self._states.clear()


_default_store: ControlPlaneStore | None = None
_default_store_lock = threading.Lock()


def build_store(backend: str) -> ControlPlaneStore:
    if backend == "memory":
        return InMemoryControlPlaneStore()
    # Imported lazily so that `memory` mode — and anything importing this module without a
    # database — does not drag in SQLAlchemy models.
    from incident_copilot.controlplane.pg_store import PostgresControlPlaneStore

    return PostgresControlPlaneStore()


def get_store() -> ControlPlaneStore:
    """The process-wide store, chosen by `control_plane_backend`."""
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            from incident_copilot.config import get_settings

            _default_store = build_store(get_settings().control_plane_backend)
        return _default_store


def set_store(store: ControlPlaneStore | None) -> None:
    """Override the process-wide store. Test-support only."""
    global _default_store
    with _default_store_lock:
        _default_store = store
