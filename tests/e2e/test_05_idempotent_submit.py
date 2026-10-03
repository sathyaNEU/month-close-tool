"""Idempotent submission: replays, an app killed right after accepting, and an app killed while
run-now is in flight after Databricks has forgotten the token (Gate 1a)."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from stacklib import User, compose, emulator, emulator_runs, wait_until

pytestmark = pytest.mark.stack


def test_twenty_deliveries_of_one_request_start_one_run() -> None:
    alice = User("alice")
    key = f"replay-{uuid.uuid4()}"
    first = alice.start(key=key)
    assert first.status_code == 202
    sequential = [alice.start(key=key) for _ in range(9)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        parallel = list(pool.map(lambda _: alice.start(key=key), range(10)))
    responses: list[httpx.Response] = [first, *sequential, *parallel]
    assert {r.status_code for r in responses} == {202}
    assert len({r.content for r in responses}) == 1  # byte-identical replays
    assert sum(r.headers.get("idempotent-replayed") == "true" for r in responses) == 19
    run_id = first.json()["run_id"]
    assert alice.wait(run_id)["state"] == "succeeded"
    assert len(emulator_runs(run_id)) == 1


def test_app_killed_right_after_accepting_a_run() -> None:
    alice = User("alice")
    run_id = alice.start().json()["run_id"]
    compose("kill", "-s", "SIGKILL", "app")
    compose("up", "-d", "--wait", "--no-deps", "app")
    run = alice.wait(run_id, timeout=180)  # a claimed outbox event is redelivered after its claim TTL
    assert run["state"] == "succeeded"
    assert len(emulator_runs(run_id)) == 1


def test_app_killed_during_run_now_after_the_token_expired_finds_the_run_instead_of_resubmitting() -> None:
    emulator("POST", "/__emulator/faults", {"mode": "hold-run-now", "seconds": 60})
    emulator("POST", "/__emulator/token-ttl", {"seconds": 5})
    alice = User("alice")
    run_id = alice.start().json()["run_id"]
    wait_until(lambda: emulator_runs(run_id), 30, what="run-now to create the run")
    compose("kill", "-s", "SIGKILL", "app")  # the relay dies before it records the external run id
    emulator("POST", "/__emulator/faults", {"mode": "normal"})
    time.sleep(6)  # the emulator forgets the idempotency token (TTL 5 s)
    compose("up", "-d", "--wait", "--no-deps", "app")
    run = alice.wait(run_id, timeout=180)
    assert run["state"] == "succeeded"
    (external,) = emulator_runs(run_id)  # found by runs/list on redelivery, not submitted twice
    assert run["external_run_id"] == external["run_id"]
