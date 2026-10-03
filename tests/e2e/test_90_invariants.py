"""Invariants over everything the suite did (runs after the fault tests, before the state-bending ones):
the writer lease only moved on after the previous attempt was terminal, tokens only went up, no
external run was ever duplicated, and nothing was lost in the outbox (Gate 1a)."""

from __future__ import annotations

import itertools
import json
from collections import Counter

import pytest
from stacklib import compose, emulator_runs, psql, scalar

pytestmark = pytest.mark.stack
SMOKE_JOB_ID = 1001
POST_JOURNALS_JOB_ID = 1004  # Stage 7b: posting runs are the platform's submissions too


def test_every_grant_follows_the_previous_attempts_termination(suite_start: dict[str, int]) -> None:
    rows = psql(
        "SELECT e.id, e.event, e.fencing_token, e.run_id, e.attempt, extract(epoch FROM e.at), "
        "extract(epoch FROM a.terminal_at), extract(epoch FROM a.quarantine_until) "
        "FROM lease_events e LEFT JOIN run_attempts a ON a.run_id = e.run_id AND a.attempt = e.attempt "
        f"WHERE e.resource = 'smoke:default' AND e.id > {suite_start['lease_event_id']} ORDER BY e.id"
    )
    grants = [r for r in rows if r[1] in {"granted", "regranted"}]
    assert len(grants) >= 20, f"expected the suite to have granted the lease many times, saw {len(grants)}"
    tokens = [int(r[2]) for r in grants]
    assert tokens == sorted(set(tokens)), "fencing tokens must strictly increase"
    for previous, grant in itertools.pairwise(grants):
        terminal_at, quarantine_until, granted_at = previous[6], previous[7], float(grant[5])
        assert terminal_at, f"attempt {previous[3]}/{previous[4]} was never terminal but the lease moved on"
        assert float(terminal_at) <= granted_at
        if quarantine_until:
            assert float(quarantine_until) <= granted_at
    for release in (r for r in rows if r[1] == "released"):
        assert release[6], f"released for an attempt that is not terminal: {release}"
        assert float(release[6]) <= float(release[5])


def test_no_external_run_was_ever_duplicated() -> None:
    # The platform's own submissions are smoke and posting runs (jobs 1001 and 1004); the gate also runs ddl_apply
    # and the generator directly through the Jobs API, which are not the platform's submissions and are not counted.
    runs = [r for r in emulator_runs() if r["job_id"] in {SMOKE_JOB_ID, POST_JOURNALS_JOB_ID}]
    per_token = Counter(r["idempotency_token"] for r in runs if r["idempotency_token"])
    assert per_token
    assert max(per_token.values()) == 1
    per_attempt = Counter((r["job_parameters"]["run_id"], r["job_parameters"]["attempt"]) for r in runs)
    assert max(per_attempt.values()) == 1


def test_the_outbox_lost_nothing_and_dead_lettered_nothing() -> None:
    assert scalar("SELECT count(*) FROM outbox WHERE dead_at IS NOT NULL") == "0"
    assert scalar("SELECT count(*) FROM outbox WHERE delivered_at IS NULL") == "0"


def test_the_audit_chain_verifies_after_the_whole_suite() -> None:
    """Every state change the suite made (smoke runs, cancels, configuration changes) is in one intact chain."""
    verdict = json.loads(compose("exec", "-T", "app", "python", "-m", "close_app.audit", "verify").stdout)
    assert verdict["ok"], verdict
