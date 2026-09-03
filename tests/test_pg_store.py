"""Postgres control-plane store: durability and optimistic concurrency.

Phase 1's simulator, actions, and API routes are written against the `ControlPlaneStore`
protocol, so this swap should be invisible to them. These tests check that it is — and that the
one thing the in-memory store could not do (safe concurrent writes) now works.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy.orm import Session, sessionmaker

from incident_copilot.controlplane.actions import SetConfigValue, apply_action
from incident_copilot.controlplane.pg_store import (
    ConcurrentStateChange,
    PostgresControlPlaneStore,
)
from incident_copilot.controlplane.scenarios import get_registry
from incident_copilot.controlplane.simulator import assess_health, compute_metrics
from incident_copilot.db.models import ControlPlaneStateRow

SCENARIO = "payments_gateway_timeout"


@pytest.fixture
def store() -> PostgresControlPlaneStore:
    return PostgresControlPlaneStore()


def test_first_access_materialises_the_scenario(
    db: Session, store: PostgresControlPlaneStore
) -> None:
    state = store.get_in(db, SCENARIO)
    db.commit()

    assert state.scenario == SCENARIO
    assert state.config["gateway_timeout_ms"].value == 1000
    assert db.query(ControlPlaneStateRow).count() == 1


def test_state_survives_a_round_trip_intact(db: Session, store: PostgresControlPlaneStore) -> None:
    """Full fidelity through JSON matters — the simulator reads every field back."""
    original = get_registry().get(SCENARIO).initial_state()
    store.put_in(db, original)
    db.commit()

    assert store.get_in(db, SCENARIO) == original


def test_writes_are_visible_to_a_separate_session(
    db: Session, store: PostgresControlPlaneStore, session_factory: sessionmaker[Session]
) -> None:
    """The property the in-memory store could not provide: state shared across processes."""
    state = store.get_in(db, SCENARIO)
    fixed = apply_action(state, SetConfigValue(key="gateway_timeout_ms", value=2000)).state
    store.put_in(db, fixed)
    db.commit()

    with session_factory() as other:
        seen = store.get_in(other, SCENARIO)
        assert seen.config["gateway_timeout_ms"].value == 2000


def test_remediation_is_observable_through_the_simulator(
    db: Session, store: PostgresControlPlaneStore
) -> None:
    """End to end: persisted state still drives derived metrics."""
    scenario = get_registry().get(SCENARIO)
    state = store.get_in(db, SCENARIO)
    assert not assess_health(scenario, state).healthy
    assert compute_metrics(scenario, state).error_rate == pytest.approx(0.124)

    fixed = apply_action(state, SetConfigValue(key="gateway_timeout_ms", value=2000)).state
    store.put_in(db, fixed)
    db.commit()

    reloaded = store.get_in(db, SCENARIO)
    assert assess_health(scenario, reloaded).healthy
    assert compute_metrics(scenario, reloaded).error_rate == pytest.approx(0.002)


def test_version_increments_on_each_write(db: Session, store: PostgresControlPlaneStore) -> None:
    state = store.get_in(db, SCENARIO)
    db.commit()
    assert store.version_in(db, SCENARIO) == 1

    assert store.put_in(db, state) == 2
    assert store.put_in(db, state) == 3
    db.commit()


def test_conditional_write_succeeds_on_the_expected_version(
    db: Session, store: PostgresControlPlaneStore
) -> None:
    state = store.get_in(db, SCENARIO)
    db.commit()
    version = store.version_in(db, SCENARIO)
    assert store.put_in(db, state, expected_version=version) == version + 1


def test_conditional_write_refuses_a_stale_version(
    db: Session, store: PostgresControlPlaneStore
) -> None:
    """Two workers remediating the same incident must not silently clobber each other."""
    state = store.get_in(db, SCENARIO)
    db.commit()
    stale_version = store.version_in(db, SCENARIO)

    store.put_in(db, state)  # somebody else writes first
    db.commit()

    with pytest.raises(ConcurrentStateChange, match="re-read before writing"):
        store.put_in(db, state, expected_version=stale_version)


def test_only_one_of_two_racing_conditional_writes_wins(
    db: Session, store: PostgresControlPlaneStore, session_factory: sessionmaker[Session]
) -> None:
    store.get_in(db, SCENARIO)
    db.commit()
    version = store.version_in(db, SCENARIO)

    winners: list[int] = []
    losers: list[str] = []
    barrier = threading.Barrier(4)

    def racer(value: int) -> None:
        with session_factory() as session:
            state = store.get_in(session, SCENARIO)
            mutated = apply_action(
                state, SetConfigValue(key="gateway_timeout_ms", value=value)
            ).state
            barrier.wait(timeout=10)
            try:
                winners.append(store.put_in(session, mutated, expected_version=version))
                session.commit()
            except Exception as exc:
                session.rollback()
                losers.append(type(exc).__name__)

    threads = [threading.Thread(target=racer, args=(1500 + i * 100,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(winners) == 1, f"{len(winners)} writers thought they won; losers={losers}"
    assert len(losers) == 3


def test_reset_returns_to_the_scenario_start(db: Session, store: PostgresControlPlaneStore) -> None:
    state = store.get_in(db, SCENARIO)
    store.put_in(
        db, apply_action(state, SetConfigValue(key="gateway_timeout_ms", value=2000)).state
    )
    db.commit()

    restored = store.reset_in(db, SCENARIO)
    db.commit()
    assert restored.config["gateway_timeout_ms"].value == 1000
    assert restored == get_registry().get(SCENARIO).initial_state()


def test_concurrent_first_access_does_not_duplicate(
    engine: object,
    store: PostgresControlPlaneStore,
    session_factory: sessionmaker[Session],
    db: Session,
) -> None:
    """Two workers touching an untouched scenario at once."""
    errors: list[str] = []
    barrier = threading.Barrier(4)

    def racer() -> None:
        with session_factory() as session:
            barrier.wait(timeout=10)
            try:
                store.get_in(session, SCENARIO)
                session.commit()
            except Exception as exc:
                session.rollback()
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=racer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert db.query(ControlPlaneStateRow).filter_by(scenario_key=SCENARIO).count() == 1
    assert not errors, f"first-access race raised {errors}"
