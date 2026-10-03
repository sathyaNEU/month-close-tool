"""Transactional outbox: side effects (job submission, cancellation, webhooks) leave the database only
through here, so a business change and its intent to act are committed together or not at all.

Semantics (Stage 1 design rule 3):

* ``Outbox.publish`` inserts in the caller's transaction. Request handlers never call external
  systems themselves.
* ``Relay.run_once`` claims ready rows in a short transaction (claim TTL in Postgres time, FIFO per
  aggregate, ``SKIP LOCKED`` across instances), delivers them *outside* any transaction, then marks
  them delivered. Delivery is at least once: a crash between delivery and marking means redelivery.
* Handlers must therefore be idempotent: consumers record ``(consumer, event_id)`` receipts in the
  same transaction as their effect (``consume``), or rely on a compare-and-set.
* Failures back off exponentially through ``available_at``. After ``max_attempts`` the row is
  dead-lettered, and later events for the same aggregate stay blocked until someone resolves it,
  because skipping ahead would break per-aggregate ordering.
* Topics listed in ``never_dead_letter`` are retried for as long as it takes. They are for handlers
  that end their own retry loop (the run coordinator's submit and cancel), where giving up would
  leave a writer lease held with nothing left to release it.
* Each delivery carries ``OutboxEvent.delivery`` (1 on the first try), so a handler can tell that an
  earlier delivery may already have acted before it crashed or failed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxEvent:
    event_id: UUID
    topic: str
    aggregate_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    payload_version: int = 1
    delivery: int = 1  # set by the relay: >1 means an earlier delivery may already have acted


class Outbox:
    def publish(self, tx: Connection, event: OutboxEvent) -> None:
        tx.execute(
            text(
                "INSERT INTO outbox (event_id, topic, aggregate_id, payload, payload_version) "
                "VALUES (:id, :topic, :agg, CAST(:payload AS jsonb), :v)"
            ),
            {
                "id": event.event_id,
                "topic": event.topic,
                "agg": event.aggregate_id,
                "payload": json.dumps(dict(event.payload), sort_keys=True),
                "v": event.payload_version,
            },
        )


def consume(tx: Connection, consumer: str, event_id: UUID) -> bool:
    """Record that ``consumer`` applied ``event_id``. Returns False if it already had (skip the effect)."""
    row = tx.execute(
        text(
            "INSERT INTO outbox_receipts (consumer, event_id) VALUES (:c, :e) ON CONFLICT DO NOTHING RETURNING event_id"
        ),
        {"c": consumer, "e": event_id},
    ).first()
    return row is not None


Handler = Callable[[OutboxEvent], None]


class Relay:
    def __init__(
        self,
        engine: Engine,
        handlers: Mapping[str, Handler],
        *,
        instance_id: str,
        claim_ttl: timedelta = timedelta(seconds=60),
        batch: int = 20,
        max_attempts: int = 10,
        max_backoff: timedelta = timedelta(minutes=5),
        never_dead_letter: Collection[str] = (),
    ) -> None:
        self._engine = engine
        self._handlers = dict(handlers)
        self._me = instance_id
        self._ttl_s = int(claim_ttl.total_seconds())
        self._batch = batch
        self._max_attempts = max_attempts
        self._max_backoff_s = int(max_backoff.total_seconds())
        self._durable = frozenset(never_dead_letter)

    def _claim(self) -> list[tuple[int, OutboxEvent, int]]:
        with self._engine.begin() as tx:
            rows = tx.execute(
                text(
                    """
                    UPDATE outbox SET claimed_by = :me, claim_expires_at = now() + make_interval(secs => :ttl),
                           attempts = attempts + 1
                     WHERE id IN (
                        SELECT o.id FROM outbox o
                         WHERE o.delivered_at IS NULL AND o.dead_at IS NULL AND o.available_at <= now()
                           AND (o.claim_expires_at IS NULL OR o.claim_expires_at < now())
                           AND NOT EXISTS (
                               SELECT 1 FROM outbox e
                                WHERE e.aggregate_id = o.aggregate_id AND e.id < o.id AND e.delivered_at IS NULL)
                         ORDER BY o.id
                         LIMIT :batch
                         FOR UPDATE SKIP LOCKED)
                    RETURNING id, event_id, topic, aggregate_id, payload, payload_version, attempts
                    """
                ),
                {"me": self._me, "ttl": self._ttl_s, "batch": self._batch},
            ).all()
        claimed = [
            (
                int(r.id),
                OutboxEvent(
                    r.event_id, r.topic, r.aggregate_id, dict(r.payload), int(r.payload_version), int(r.attempts)
                ),
                int(r.attempts),
            )
            for r in rows
        ]
        return sorted(claimed, key=lambda item: item[0])

    def _finish(self, row_id: int, *, error: str | None, attempts: int, topic: str) -> None:
        with self._engine.begin() as tx:
            if error is None:
                tx.execute(
                    text(
                        "UPDATE outbox SET delivered_at = now(), claimed_by = NULL, claim_expires_at = NULL, "
                        "last_error = NULL WHERE id = :id AND claimed_by = :me"
                    ),
                    {"id": row_id, "me": self._me},
                )
                return
            backoff = min(self._max_backoff_s, 2 ** min(attempts, 16))
            tx.execute(
                text(
                    """
                    UPDATE outbox SET claimed_by = NULL, claim_expires_at = NULL, last_error = :err,
                           available_at = now() + make_interval(secs => :backoff),
                           dead_at = CASE WHEN attempts >= :max AND NOT :durable THEN now() ELSE NULL END
                     WHERE id = :id AND claimed_by = :me
                    """
                ),
                {
                    "err": error[:2000],
                    "backoff": backoff,
                    "max": self._max_attempts,
                    "durable": topic in self._durable,
                    "id": row_id,
                    "me": self._me,
                },
            )

    def run_once(self) -> int:
        """Claim and deliver one batch. Returns the number of events delivered successfully."""
        delivered = 0
        for row_id, event, attempts in self._claim():
            handler = self._handlers.get(event.topic)
            if handler is None:
                self._finish(
                    row_id, error=f"no handler for topic {event.topic!r}", attempts=attempts, topic=event.topic
                )
                continue
            try:
                handler(event)
            except Exception as exc:  # delivery failures are recorded and retried, never raised
                log.warning("outbox delivery failed", extra={"event_id": str(event.event_id), "topic": event.topic})
                self._finish(row_id, error=f"{type(exc).__name__}: {exc}", attempts=attempts, topic=event.topic)
                continue
            self._finish(row_id, error=None, attempts=attempts, topic=event.topic)
            delivered += 1
        return delivered
