"""The hash-chained audit log (Gate 4): contiguous appends, exact round trips, rollback, concurrency, and
tampering by the migrator role detected by the verifier."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from sqlalchemy import Engine, text

from close_app import audit

pytestmark = pytest.mark.pg


def _append(engine: Engine, n: int = 1, **payload: Any) -> None:
    with engine.begin() as tx:
        for i in range(n):
            audit.append(tx, audit.Event("u1", "test.event", entity_ids=("US01",), target=f"t{i}", payload=payload))


def _verify(engine: Engine) -> audit.Verdict:
    with engine.connect() as conn:
        return audit.verify(conn)


def test_appends_are_contiguous_and_verify(engine: Engine) -> None:
    _append(engine, 3)
    _append(engine, 2, note="second batch")
    verdict = _verify(engine)
    assert verdict.ok, verdict
    assert (verdict.events, verdict.head_seq) == (5, 5)


def test_payloads_round_trip_exactly(engine: Engine) -> None:
    payload = {
        "big": 2**53 - 1,
        "negative": -(2**53 - 1),
        "text": 'naïve café \u2028 \U0001f600 "quoted" \\ back',
        "nested": {"b": [1, {"c": None, "a": True}], "a": ""},
        "empty": {},
    }
    _append(engine, 1, **payload)
    assert _verify(engine).ok
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT payload FROM audit_events")).scalar_one()
    assert stored == payload


@pytest.mark.parametrize(
    "payload",
    [{"x": 1.5}, {"x": 2**53}, {"x": "nul\x00"}, {"x": {"\x00k": 1}}, {"x": b"bytes"}],
)
def test_payloads_that_would_not_round_trip_are_refused(engine: Engine, payload: dict[str, Any]) -> None:
    with pytest.raises(audit.PayloadError), engine.begin() as tx:
        audit.append(tx, audit.Event("u1", "test.event", payload=payload))
    assert _verify(engine).events == 0


def test_a_rolled_back_change_leaves_no_event_and_no_gap(engine: Engine) -> None:
    _append(engine, 1)

    def failing_change() -> None:
        with engine.begin() as tx:
            audit.append(tx, audit.Event("u1", "test.event"))
            raise RuntimeError("the change failed after its event was appended")

    with pytest.raises(RuntimeError):
        failing_change()
    _append(engine, 1)
    verdict = _verify(engine)
    assert verdict.ok
    assert verdict.events == 2


def test_concurrent_appends_never_fork_or_gap(engine: Engine) -> None:
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(10):
                with engine.begin() as tx:
                    audit.append(tx, audit.Event(f"w{i}", "test.event", target=str(j)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    verdict = _verify(engine)
    assert verdict.ok, verdict
    assert verdict.events == 80


TAMPERS = {
    "edit a payload": 'UPDATE audit_events SET payload = \'{"note": "changed"}\' WHERE seq = 2',
    "edit an actor": "UPDATE audit_events SET actor = 'someone else' WHERE seq = 3",
    "delete an event": "DELETE FROM audit_events WHERE seq = 2",
    "truncate the tail": "DELETE FROM audit_events WHERE seq >= 4",
    "reorder two events": (
        "UPDATE audit_events SET seq = CASE seq WHEN 2 THEN 1002 WHEN 3 THEN 2 END WHERE seq IN (2, 3); "
        "UPDATE audit_events SET seq = 3 WHERE seq = 1002"
    ),
    "move the head back": "UPDATE audit_chain_head SET last_seq = 3",
}


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
def test_tampering_as_the_migrator_is_detected(engine: Engine, admin_engine: Engine, tamper: str) -> None:
    _append(engine, 5, note="original")
    assert _verify(engine).ok
    with admin_engine.begin() as tx:
        for statement in TAMPERS[tamper].split("; "):
            tx.execute(text(statement))
    verdict = _verify(engine)
    assert not verdict.ok, tamper
    assert verdict.first_bad_seq is not None


@pytest.mark.xfail(strict=True, reason="a database owner can rewrite the tail and recompute the chain (ADR-0014)")
def test_a_recomputed_chain_is_detected(engine: Engine, admin_engine: Engine) -> None:
    _append(engine, 3, note="original")
    with admin_engine.begin() as tx:
        rows = tx.execute(text("SELECT * FROM audit_events ORDER BY seq")).mappings().all()
        prev = audit.GENESIS
        for r in rows:
            event = audit.Event(r["actor"], r["action"], r["outcome"], tuple(r["entity_ids"]), r["target"])
            payload = {"note": "rewritten"}
            record = audit._record(r["seq"], r["recorded_at"], event, r["request_id"], payload)
            digest = audit.record_hash(prev, record)
            tx.execute(
                text(
                    "UPDATE audit_events SET payload = CAST(:p AS jsonb), prev_hash = :prev, hash = :h WHERE seq = :s"
                ),
                {"p": '{"note": "rewritten"}', "prev": prev, "h": digest, "s": r["seq"]},
            )
            prev = digest
        tx.execute(text("UPDATE audit_chain_head SET last_hash = :h"), {"h": prev})
    assert not _verify(engine).ok


def test_the_cli_verifies_and_reports(engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    _append(engine, 2)
    assert audit.main(["verify"], engine) == 0
    assert '"ok": true' in capsys.readouterr().out
    assert audit.main(["nonsense"], engine) == 2
