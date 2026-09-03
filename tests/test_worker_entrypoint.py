"""The worker entry point must actually have handlers.

Run in a subprocess on purpose. Handler registration is a side effect of import, and the rest of
the suite imports handler modules directly — so an in-process test cannot tell the difference
between "the worker registers its handlers" and "some other test imported them first".

This is the test that was missing. Without it, the worker claimed a real `investigate` job and
buried it with `no handler registered for job kind 'investigate'. Registered: (none)`, while
every in-process test passed.
"""

from __future__ import annotations

import subprocess
import sys


def run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_load_all_registers_the_investigate_handler() -> None:
    out = run("from incident_copilot.jobs.handlers import load_all; print(','.join(load_all()))")
    assert "investigate" in out.split(",")


def test_a_bare_import_registers_nothing() -> None:
    """The failure mode being guarded against: importing the package alone is not enough."""
    out = run(
        "from incident_copilot.jobs import handlers; print(','.join(handlers.registered_kinds()))"
    )
    assert out == "", f"expected an empty registry without load_all(), got {out!r}"


def test_load_all_is_idempotent() -> None:
    """Called twice it must not raise on duplicate registration."""
    out = run(
        "from incident_copilot.jobs.handlers import load_all\n"
        "load_all()\nprint(','.join(load_all()))"
    )
    assert "investigate" in out.split(",")


def test_every_handler_module_is_discovered() -> None:
    """Discovery, not an import list — a handler added later needs no wiring."""
    out = run(
        "import pkgutil\n"
        "from incident_copilot.jobs import handlers\n"
        "from incident_copilot.jobs.handlers import load_all\n"
        "mods = {m.name for m in pkgutil.iter_modules(handlers.__path__)}\n"
        "load_all()\n"
        "print(len(mods), len(handlers.registered_kinds()))"
    )
    modules, kinds = (int(x) for x in out.split())
    assert kinds >= modules, f"{modules} handler module(s) but only {kinds} registered kind(s)"
