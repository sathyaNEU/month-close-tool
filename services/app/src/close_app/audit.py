"""The hash-chained audit log (Stage 4) and its verifier.

    python -m close_app.audit verify     # exit 0 when the chain is intact, 1 at the first broken position

- **Append.** An event is appended in the transaction that makes the change it records. The chain head
  (``last_seq``, ``last_hash``) is locked as the **last** lock the transaction takes, so the event gets the next
  contiguous position even when other transactions roll back, and appends never deadlock with the leases,
  runs or outbox rows a request holds first.
- **The record.** The event is stamped with ``clock_timestamp()`` after the lock. Its hash is SHA-256 of the
  previous hash and the RFC 8785 JSON of the record, with the timestamp in one UTC format.
- **Payloads** are limited to what survives ``jsonb`` unchanged (strings without NUL, JSON-safe integers,
  booleans, null, lists and string-keyed objects), so the verifier re-derives each hash from what Postgres
  returns.
- **Verification** needs contiguous positions from 1, each event linked to the previous hash, each hash
  matching its record, and the head matching the last event. Time order is not checked: positions carry
  the order.
- **The residual risk.** A database owner who rewrites the tail and recomputes every hash is not detected
  without an external checkpoint of the head (Stage 11).
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import Connection, Engine, text

from close_contracts.canonical_json import MAX_SAFE_INTEGER, canonical_json

GENESIS: Final = bytes(32)
OUTCOMES: Final = frozenset({"ok", "denied"})


@dataclass(frozen=True)
class Event:
    actor: str
    action: str
    outcome: str = "ok"
    entity_ids: tuple[str, ...] = ()  # empty for a global event
    target: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


class PayloadError(ValueError):
    pass


def _check(value: Any, path: str = "payload") -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise PayloadError(f"{path}: integers must be JSON-safe")
        return
    if isinstance(value, str):
        if "\x00" in value:
            raise PayloadError(f"{path}: NUL cannot be stored")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or "\x00" in key:
                raise PayloadError(f"{path}: keys must be strings without NUL")
            _check(item, f"{path}.{key}")
        return
    raise PayloadError(f"{path}: {type(value).__name__} is not allowed (use strings for decimals and dates)")


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record_hash(prev_hash: bytes, record: Mapping[str, Any]) -> bytes:
    return hashlib.sha256(prev_hash + canonical_json(dict(record))).digest()


def _record(seq: int, at: datetime, event: Event, request_id: str | None, payload: Any) -> dict[str, Any]:
    return {
        "seq": seq,
        "recorded_at": _stamp(at),
        "actor": event.actor,
        "action": event.action,
        "outcome": event.outcome,
        "entity_ids": sorted(event.entity_ids),
        "target": event.target,
        "request_id": request_id,
        "payload": payload,
    }


def append(tx: Connection, event: Event, request_id: str | None = None) -> int:
    """Append ``event`` in the caller's transaction and return its position."""
    if event.outcome not in OUTCOMES:
        raise PayloadError(f"unknown outcome {event.outcome}")
    payload = dict(event.payload)
    _check(payload)
    head = tx.execute(text("SELECT last_seq, last_hash FROM audit_chain_head WHERE id = 1 FOR UPDATE")).one()
    at = tx.execute(text("SELECT clock_timestamp()")).scalar_one()
    seq = int(head.last_seq) + 1
    digest = record_hash(bytes(head.last_hash), _record(seq, at, event, request_id, payload))
    tx.execute(
        text(
            "INSERT INTO audit_events (seq, recorded_at, actor, action, outcome, entity_ids, target, request_id, "
            "payload, prev_hash, hash) VALUES (:seq, :at, :actor, :action, :outcome, :entities, :target, :rid, "
            "CAST(:payload AS jsonb), :prev, :hash)"
        ),
        {
            "seq": seq,
            "at": at,
            "actor": event.actor,
            "action": event.action,
            "outcome": event.outcome,
            "entities": sorted(event.entity_ids),
            "target": event.target,
            "rid": request_id,
            "payload": json.dumps(payload, ensure_ascii=False),
            "prev": bytes(head.last_hash),
            "hash": digest,
        },
    )
    tx.execute(text("UPDATE audit_chain_head SET last_seq = :s, last_hash = :h WHERE id = 1"), {"s": seq, "h": digest})
    return seq


@dataclass(frozen=True)
class Verdict:
    ok: bool
    events: int
    head_seq: int
    first_bad_seq: int | None = None
    reason: str | None = None

    def as_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)


def verify(conn: Connection, batch: int = 5000) -> Verdict:
    head = conn.execute(text("SELECT last_seq, last_hash FROM audit_chain_head WHERE id = 1")).one()
    prev, expected, count = GENESIS, 1, 0
    after = 0
    while True:
        rows = conn.execute(
            text(
                "SELECT seq, recorded_at, actor, action, outcome, entity_ids, target, request_id, payload, "
                "prev_hash, hash FROM audit_events WHERE seq > :after ORDER BY seq LIMIT :n"
            ),
            {"after": after, "n": batch},
        ).all()
        if not rows:
            break
        for r in rows:
            if r.seq != expected:
                return Verdict(False, count, int(head.last_seq), expected, f"position {expected} is missing")
            if bytes(r.prev_hash) != prev:
                return Verdict(False, count, int(head.last_seq), r.seq, "not linked to the previous event")
            event = Event(r.actor, r.action, r.outcome, tuple(r.entity_ids), r.target)
            if record_hash(prev, _record(r.seq, r.recorded_at, event, r.request_id, r.payload)) != bytes(r.hash):
                return Verdict(False, count, int(head.last_seq), r.seq, "the record does not match its hash")
            prev, expected, count, after = bytes(r.hash), expected + 1, count + 1, r.seq
    if int(head.last_seq) != count or bytes(head.last_hash) != prev:
        return Verdict(False, count, int(head.last_seq), count + 1, "the head does not match the last event")
    return Verdict(True, count, int(head.last_seq))


def main(argv: list[str], engine: Engine | None = None) -> int:
    if argv != ["verify"]:
        sys.stderr.write("usage: python -m close_app.audit verify\n")
        return 2
    if engine is None:
        from close_platform.db.credentials import StaticCredentialProvider
        from close_platform.db.engine import make_engine

        engine = make_engine(StaticCredentialProvider.from_env(), pool_size=1, max_overflow=0)
    with engine.connect() as conn:
        verdict = verify(conn)
    sys.stdout.write(verdict.as_json() + "\n")
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
