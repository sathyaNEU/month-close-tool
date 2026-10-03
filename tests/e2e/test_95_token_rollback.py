"""A control-plane database restored from an older backup hands out fencing tokens that are lower than
claims already in the lake. Every job then fences itself (safe, nothing is written) until an operator
raises the lease token above the highest claim, which is the documented recovery (ADR 0007).

This bends the lease history on purpose, so it runs after test_90 checked the invariants."""

from __future__ import annotations

import pytest
from stacklib import User, lease_token, psql

pytestmark = pytest.mark.stack


def test_rolled_back_tokens_are_fenced_until_raised_above_the_lake() -> None:
    alice = User("alice")
    current = lease_token()
    assert current > 1
    psql("UPDATE leases SET fencing_token = 0 WHERE resource = 'smoke:default'")  # the restore
    fenced = alice.wait(alice.start().json()["run_id"])
    assert fenced["state"] == "failed"
    assert fenced["fencing_token"] == 1
    assert fenced["error"].startswith("fenced: a newer writer holds the lease")
    assert fenced["result"]["fenced"] is True
    raised = current + 10
    psql(f"UPDATE leases SET fencing_token = {raised} WHERE resource = 'smoke:default'")  # the recovery
    ok = alice.wait(alice.start().json()["run_id"])
    assert ok["state"] == "succeeded"
    assert ok["fencing_token"] == raised + 1
