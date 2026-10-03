from __future__ import annotations

import threading
import uuid

import pytest
from pgtestkit import insert_attempt, insert_run
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from close_platform.leases import LeaseBusyError, LeaseManager, LeaseStateError, validate_resource

pytestmark = pytest.mark.pg
LM = LeaseManager("test")


def test_acquire_release_cycle_increments_token_every_grant(engine: Engine) -> None:
    with engine.begin() as tx:
        run1 = insert_run(tx)
        g1 = LM.acquire(tx, "smoke:default", run1)
        insert_attempt(tx, run1, 1, g1.fencing_token, state="terminated")
    assert g1.fencing_token == 1
    with engine.begin() as tx:
        LM.release(tx, g1, reason="done")
        run2 = insert_run(tx)
        g2 = LM.acquire(tx, "smoke:default", run2)
    assert g2.fencing_token == 2
    with engine.connect() as conn:
        events = conn.execute(text("SELECT event, fencing_token FROM lease_events ORDER BY id")).all()
    assert [(e.event, e.fencing_token) for e in events] == [("granted", 1), ("released", 1), ("granted", 2)]


def test_busy_lease_is_refused_with_the_holder(engine: Engine) -> None:
    with engine.begin() as tx:
        run1, run2 = insert_run(tx), insert_run(tx)
        g1 = LM.acquire(tx, "match_set:ms-1", run1)
        with pytest.raises(LeaseBusyError) as err:
            LM.acquire(tx, "match_set:ms-1", run2)
    assert err.value.holder == g1


def test_release_requires_a_terminal_attempt_and_no_quarantine(engine: Engine) -> None:
    with engine.begin() as tx:
        run = insert_run(tx)
        g = LM.acquire(tx, "smoke:default", run)
        insert_attempt(tx, run, 1, g.fencing_token, state="running")
    with engine.begin() as tx, pytest.raises(LeaseStateError, match="not terminal"):
        LM.release(tx, g, reason="too early")
    with engine.begin() as tx:
        tx.execute(
            text(
                "UPDATE run_attempts SET state = 'terminated', quarantine_until = now() + interval '1 hour' "
                "WHERE run_id = :id"
            ),
            {"id": run},
        )
    with engine.begin() as tx, pytest.raises(LeaseStateError, match="quarantine"):
        LM.release(tx, g, reason="quarantined")
    with engine.begin() as tx:
        tx.execute(
            text("UPDATE run_attempts SET quarantine_until = now() - interval '1 second' WHERE run_id = :id"),
            {"id": run},
        )
        LM.release(tx, g, reason="after quarantine")
    with engine.connect() as conn:
        assert LM.holder(conn, "smoke:default") is None


def test_regrant_moves_to_next_attempt_with_a_new_token(engine: Engine) -> None:
    with engine.begin() as tx:
        run = insert_run(tx)
        g1 = LM.acquire(tx, "smoke:default", run)
        g2 = LM.regrant(tx, g1, next_attempt=2)
        assert (g2.attempt, g2.fencing_token) == (2, g1.fencing_token + 1)
        with pytest.raises(LeaseStateError):
            LM.regrant(tx, g1, next_attempt=3)  # g1 is no longer the holder


def test_force_release_bumps_the_token_and_is_audited(engine: Engine) -> None:
    with engine.begin() as tx:
        run = insert_run(tx)
        g = LM.acquire(tx, "smoke:default", run)
        token = LM.force_release(tx, "smoke:default", actor="ops@example.com", reason="stuck run INC-42")
    assert token == g.fencing_token + 1
    with engine.connect() as conn:
        last = conn.execute(text("SELECT event, actor, reason FROM lease_events ORDER BY id DESC LIMIT 1")).one()
    assert (last.event, last.actor, last.reason) == ("force_released", "ops@example.com", "stuck run INC-42")
    with engine.begin() as tx, pytest.raises(ValueError, match="reason"):
        LM.force_release(tx, "smoke:default", actor="ops", reason=" ")


def test_twenty_parallel_acquires_have_exactly_one_winner(engine: Engine) -> None:
    results: list[str] = []
    barrier = threading.Barrier(20)

    def worker() -> None:
        with engine.begin() as tx:
            run = insert_run(tx, resource="match_set:parallel")
            barrier.wait()
            try:
                LM.acquire(tx, "match_set:parallel", run)
                results.append("won")
            except LeaseBusyError:
                results.append("busy")

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count("won") == 1
    assert results.count("busy") == 19
    with engine.connect() as conn:
        assert LM.current_token(conn, "match_set:parallel") == 1


def test_application_role_cannot_rewrite_the_lease_audit_trail(engine: Engine) -> None:
    with engine.begin() as tx:
        run = insert_run(tx)
        LM.acquire(tx, "smoke:default", run)
    for statement in ("UPDATE lease_events SET reason = 'x'", "DELETE FROM lease_events"):
        with engine.begin() as tx, pytest.raises(ProgrammingError, match="permission denied"):
            tx.execute(text(statement))


@pytest.mark.parametrize("bad", ["Smoke:x", "smoke", "smoke:", "smoke:a b", "x" * 40 + ":y", "smoke:" + "a" * 97])
def test_resource_names_are_validated(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid lease resource"):
        validate_resource(bad)


def test_unknown_run_cannot_hold_a_lease(engine: Engine) -> None:
    with engine.begin() as tx, pytest.raises(Exception, match="foreign key"):
        LM.acquire(tx, "smoke:default", uuid.uuid4())
