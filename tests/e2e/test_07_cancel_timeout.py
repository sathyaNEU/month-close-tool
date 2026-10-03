"""Cancel and timeout end the run promptly and leave no Spark processes behind (Gate 1a)."""

from __future__ import annotations

import time

import pytest
from stacklib import User, emulator, emulator_runs, hold, job_processes, wait_reached, wait_until

pytestmark = pytest.mark.stack


def test_cancel_terminates_within_30_seconds_without_orphans() -> None:
    hold("after_claim")
    alice = User("alice")
    run_id = alice.start().json()["run_id"]
    wait_reached("after_claim")
    started = time.monotonic()
    assert alice.cancel(run_id).status_code == 202
    run = alice.wait(run_id, timeout=60)
    elapsed = time.monotonic() - started
    assert run["state"] == "cancelled"
    assert elapsed <= 30, f"cancel took {elapsed:.1f}s"
    (external,) = emulator_runs(run_id)
    assert external["result_state"] == "CANCELED"
    wait_until(lambda: not job_processes(), 15, what="the cancelled job's processes to exit")
    print(f"cancel to cancelled: {elapsed:.1f}s")


def test_timeout_ends_the_run_within_timeout_plus_10_seconds_without_orphans() -> None:
    timeout_s = 20
    emulator("POST", "/__emulator/job-timeout", {"job_id": 1001, "seconds": timeout_s})
    hold("after_claim")  # the job would wait forever; only the timeout can end it
    alice = User("alice")
    run_id = alice.start().json()["run_id"]
    run = alice.wait(run_id, timeout=timeout_s + 60)
    assert run["state"] == "failed"
    (external,) = emulator_runs(run_id)
    assert external["result_state"] == "TIMEDOUT"
    took = external["end_time"] - external["start_time"]
    assert took <= timeout_s + 10, f"run ended {took:.1f}s after it started"
    wait_until(lambda: not job_processes(), 15, what="the timed-out job's processes to exit")
    print(f"timeout {timeout_s}s, run ended after {took:.1f}s")
