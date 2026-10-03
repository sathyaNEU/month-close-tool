"""Outbox under failure: 100 events across crash points, 0 lost and 0 double-applied (Gate 1a)."""

from __future__ import annotations

import threading
import uuid
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from close_platform.outbox import Outbox, OutboxEvent, Relay, consume

pytestmark = pytest.mark.pg


class CrashError(Exception):
    pass


def _publish(engine: Engine, n: int, aggregates: int = 10) -> list[uuid.UUID]:
    ids = []
    with engine.begin() as tx:
        for i in range(n):
            event_id = uuid.uuid4()
            Outbox().publish(tx, OutboxEvent(event_id, "test.apply", f"agg-{i % aggregates}", {"seq": i}))
            ids.append(event_id)
    return ids


def _applied(engine: Engine) -> Counter[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT event_id FROM outbox_receipts WHERE consumer = 'test'")).all()
    return Counter(str(r.event_id) for r in rows)


def test_hundred_events_with_crashes_are_applied_exactly_once(engine: Engine) -> None:
    ids = _publish(engine, 100)
    effects: Counter[str] = Counter()
    lock = threading.Lock()
    crash_after_effect = {str(i) for i in ids[::7]}  # effect committed, then the relay dies before marking
    crash_before_effect = {str(i) for i in ids[3::11]}  # handler fails before applying anything
    crashed: set[str] = set()

    def handler(event: OutboxEvent) -> None:
        key = str(event.event_id)
        if key in crash_before_effect and key not in crashed:
            crashed.add(key)
            raise CrashError("before effect")
        with engine.begin() as tx:
            if consume(tx, "test", event.event_id):
                with lock:
                    effects[key] += 1
        if key in crash_after_effect and key not in crashed:
            crashed.add(key)
            raise CrashError("after effect, before the relay marks delivery")

    relay = Relay(
        engine,
        {"test.apply": handler},
        instance_id="r1",
        claim_ttl=timedelta(seconds=1),
        max_backoff=timedelta(seconds=1),
    )
    # Crash point 3: a relay claims a batch and dies without delivering; its claim must expire.
    dead = Relay(engine, {"test.apply": handler}, instance_id="dead", claim_ttl=timedelta(seconds=1), batch=5)
    dead._claim()
    with engine.begin() as tx:
        tx.execute(text("UPDATE outbox SET available_at = now() WHERE delivered_at IS NULL"))

    for _ in range(200):
        relay.run_once()
        with engine.begin() as tx:  # skip backoff waits so the test stays fast
            tx.execute(text("UPDATE outbox SET available_at = now() WHERE delivered_at IS NULL AND dead_at IS NULL"))
            tx.execute(
                text("UPDATE outbox SET claim_expires_at = now() - interval '1 second' WHERE claimed_by = 'dead'")
            )
        with engine.connect() as conn:
            if conn.execute(text("SELECT count(*) FROM outbox WHERE delivered_at IS NULL")).scalar_one() == 0:
                break

    assert set(_applied(engine)) == {str(i) for i in ids}  # 0 lost
    assert max(effects.values()) == 1  # 0 double-applied
    assert sum(effects.values()) == 100
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM outbox WHERE dead_at IS NOT NULL")).scalar_one() == 0


def test_events_are_delivered_in_order_per_aggregate(engine: Engine) -> None:
    _publish(engine, 30, aggregates=3)
    seen: dict[str, list[int]] = {}

    def handler(event: OutboxEvent) -> None:
        seen.setdefault(event.aggregate_id, []).append(int(event.payload["seq"]))

    relay = Relay(engine, {"test.apply": handler}, instance_id="r1", batch=4)
    while relay.run_once():
        pass
    assert all(seqs == sorted(seqs) for seqs in seen.values())
    assert sum(len(v) for v in seen.values()) == 30


def test_poison_event_is_dead_lettered_and_blocks_its_aggregate_only(engine: Engine) -> None:
    ids = _publish(engine, 4, aggregates=2)  # agg-0: ids[0], ids[2]; agg-1: ids[1], ids[3]
    poison = ids[0]

    def handler(event: OutboxEvent) -> None:
        if event.event_id == poison:
            raise ValueError("bad payload")

    relay = Relay(engine, {"test.apply": handler}, instance_id="r1", max_attempts=3, max_backoff=timedelta(seconds=1))
    for _ in range(10):
        relay.run_once()
        with engine.begin() as tx:
            tx.execute(text("UPDATE outbox SET available_at = now() WHERE delivered_at IS NULL AND dead_at IS NULL"))
    with engine.connect() as conn:
        rows = {
            r.event_id: r
            for r in conn.execute(text("SELECT event_id, delivered_at, dead_at, last_error FROM outbox")).all()
        }
    assert rows[poison].dead_at is not None
    assert "bad payload" in rows[poison].last_error
    assert rows[ids[2]].delivered_at is None  # same aggregate: blocked behind the dead event
    assert rows[ids[1]].delivered_at is not None
    assert rows[ids[3]].delivered_at is not None


def test_unknown_topic_is_recorded_not_dropped(engine: Engine) -> None:
    with engine.begin() as tx:
        Outbox().publish(tx, OutboxEvent(uuid.uuid4(), "nobody.listens", "agg", {}))
    Relay(engine, {}, instance_id="r1").run_once()
    with engine.connect() as conn:
        row = conn.execute(text("SELECT delivered_at, last_error FROM outbox")).one()
    assert row.delivered_at is None
    assert "no handler" in row.last_error
