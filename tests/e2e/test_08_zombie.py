"""A zombie writer holding a stale fencing token is fenced before it commits (Gate 1a).

The zombie runs in the `faults` container with the same spark-submit command as a real run, claims
the smoke lease's old token, and waits. A newer run then takes the lease and commits. Released, the
zombie finds the newer claim, exits 3 and commits nothing. The residual window (a zombie that has
already passed its last check) is pinned by a strict xfail in engine/tests/spark/test_fence.py.
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest
from stacklib import COMPOSE, LAKE_ROOT, REPO, User, compose, hold, release, smoke_commits, wait_reached

pytestmark = pytest.mark.stack

POINT = "zombie_after_claim"


@pytest.fixture
def faults_container() -> object:
    compose("--profile", "faults", "up", "-d", "--wait", "--no-deps", "faults")
    yield None
    compose("--profile", "faults", "stop", "faults")


def test_a_stale_writer_is_fenced_and_commits_nothing(faults_container: object) -> None:
    alice = User("alice")
    a = alice.wait(alice.start().json()["run_id"])
    assert a["state"] == "succeeded"
    stale = a["fencing_token"]
    commits_before = len(smoke_commits())
    hold(POINT)
    zombie = subprocess.Popen(
        [
            *COMPOSE, "exec", "-T", "-e", f"CLOSE_FAULTS=after_claim=/faults/{POINT}", "faults",
            "python", "-m", "close_jobs_emulator.submit", "smoke",
            "--run-id", uuid.UUID(a["run_id"]).hex, "--attempt", "1", "--lease-resource", "smoke:default",
            "--fencing-token", str(stale), "--requested-at", a["created_at"], "--root-uri", LAKE_ROOT,
            "--request-id", "zombie", "--rows", "777",
        ],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        wait_reached(POINT, timeout=180)  # the zombie has claimed the stale token and is waiting
        b = alice.wait(alice.start().json()["run_id"])
        assert b["state"] == "succeeded"
        assert b["fencing_token"] == stale + 1
    finally:
        release(POINT)
    out, err = zombie.communicate(timeout=180)
    assert zombie.returncode == 3, err[-3000:]
    line = next(ln for ln in out.splitlines() if ln.startswith("CLOSE_JOB_RESULT "))
    result = json.loads(line.removeprefix("CLOSE_JOB_RESULT "))
    assert result["fenced"] is True
    assert result["fenced_stage"] == "before_commit"
    assert result["highest_token"] == stale + 1
    new_commits = smoke_commits()[commits_before:]
    assert [c["userMetadata"]["fencing_token"] for c in new_commits] == [stale + 1]  # only the newer run
    assert all(c["operationMetrics"].get("numOutputRows") != "777" for c in smoke_commits())
