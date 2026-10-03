"""End-to-end suite against the local stack with the e2e overrides (``make test-e2e``).

Before each test the stack must be idle (no active run, no held lease), every pause point released
and the emulator back to normal, so the tests are independent of each other's order. Files run in
name order; the last ones deliberately bend state (a simulated database restore).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from stacklib import FAULTS, POINTS, compose, emulator_normal, release, scalar, wait_idle

pytestmark = pytest.mark.stack


@pytest.fixture(scope="session", autouse=True)
def stack() -> None:
    # The suite kills containers and bends database state, so a bare `pytest` never runs it.
    if os.environ.get("CLOSE_E2E") != "1":
        pytest.skip("end-to-end suite: run it with `make test-e2e` (sets CLOSE_E2E=1)")
    FAULTS.mkdir(parents=True, exist_ok=True)
    for point in POINTS:
        release(point)
    compose("up", "-d", "--wait", timeout=900)  # applies compose.e2e.yaml if `make up` started the stack


@pytest.fixture(scope="session")
def suite_start(stack: None) -> dict[str, int]:
    """Where this session's history begins, so invariants ignore earlier sessions on the same volumes."""
    return {"lease_event_id": int(scalar("SELECT COALESCE(max(id), 0) FROM lease_events"))}


@pytest.fixture(autouse=True)
def settled(suite_start: dict[str, int]) -> Iterator[None]:
    wait_idle()
    yield
    for point in POINTS:
        release(point)
    emulator_normal()
