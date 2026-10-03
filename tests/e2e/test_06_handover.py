"""Hand-over under failure: the lease moves on only after the old attempt is terminal, whoever dies
and whenever (Gate 1a). Kill points 1 and 2 (after accepting, during run-now) are in test_05; the
invariant over every grant of the whole suite is checked in test_90."""

from __future__ import annotations

import time

import pytest
from stacklib import (
    User,
    compose,
    emulator,
    emulator_runs,
    hold,
    psql,
    release,
    scalar,
    wait_idle,
    wait_reached,
    wait_until,
)

pytestmark = pytest.mark.stack


def test_kill_point_3_app_killed_while_the_job_runs() -> None:
    hold("after_claim")
    alice = User("alice")
    run_id = alice.start().json()["run_id"]
    wait_reached("after_claim")
    compose("kill", "-s", "SIGKILL", "app")
    release("after_claim")
    compose("up", "-d", "--wait", "--no-deps", "app")
    assert alice.wait(run_id, timeout=180)["state"] == "succeeded"


def test_kill_point_4_jobs_service_restarted_mid_run_quarantines_the_lease() -> None:
    hold("after_claim")
    alice = User("alice")
    run_id = alice.start().json()["run_id"]
    wait_reached("after_claim")
    compose("restart", "jobs", timeout=180)  # the run is orphaned and ends INTERNAL_ERROR
    release("after_claim")
    run = alice.wait(run_id, timeout=180)
    assert run["state"] == "failed"
    (attempt,) = psql(
        "SELECT last_status->>'termination_type', quarantine_until IS NOT NULL FROM run_attempts "
        f"WHERE run_id = '{run_id}'"
    )
    assert attempt == ["INTERNAL_ERROR", "t"]
    wait_idle(120)
    released_after_quarantine = scalar(
        "SELECT bool_and(e.at >= a.quarantine_until) FROM lease_events e JOIN run_attempts a "
        f"ON a.run_id = e.run_id AND a.attempt = e.attempt WHERE e.run_id = '{run_id}' AND e.event = 'released'"
    )
    assert released_after_quarantine == "t"
    nxt = alice.start()
    assert nxt.status_code == 202
    assert alice.wait(nxt.json()["run_id"])["state"] == "succeeded"


def test_kill_point_5_job_finishes_while_the_app_is_down() -> None:
    hold("before_commit")
    alice = User("alice")
    run_id = alice.start().json()["run_id"]
    wait_reached("before_commit")
    compose("kill", "-s", "SIGKILL", "app")
    release("before_commit")
    wait_until(
        lambda: [r for r in emulator_runs(run_id) if r["state_v2"] == "TERMINATED"], 120, what="the job to finish"
    )
    compose("up", "-d", "--wait", "--no-deps", "app")
    assert alice.wait(run_id, timeout=120)["state"] == "succeeded"


def test_a_jobs_api_outage_freezes_everything_until_it_ends() -> None:
    hold("after_claim")
    alice, bob = User("alice"), User("bob")
    run_id = alice.start().json()["run_id"]
    wait_reached("after_claim")
    alice.wait(run_id, states=frozenset({"running"}), timeout=60)
    emulator("POST", "/__emulator/faults", {"mode": "unavailable"})
    try:
        release("after_claim")
        wait_until(
            lambda: [r for r in emulator_runs(run_id) if r["state_v2"] == "TERMINATED"],
            120,
            what="the job to finish during the outage",
        )
        time.sleep(5)  # several poll intervals: nothing may move while the status is unknown
        assert alice.get(run_id)["state"] == "running"
        assert scalar("SELECT holder_run_id FROM leases WHERE resource = 'smoke:default'") == run_id
        assert bob.start().status_code == 409
    finally:
        emulator("POST", "/__emulator/faults", {"mode": "normal"})
    assert alice.wait(run_id, timeout=120)["state"] == "succeeded"
