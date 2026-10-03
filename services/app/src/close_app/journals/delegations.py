"""Delegation: an approver hands their approval authority in named entities to a colleague for a date window.

- **Who.** The delegator needs ``journal.delegate`` in each entity. The delegate must already hold a role there,
  so they can see the journals, and nobody can delegate to themselves (refused and audited).
- **Using it.** A delegate approves as themselves, on behalf of the delegator. Both people count for separation of
  duties: neither may have prepared the journal or approved another level of it.
- **Ending it.** A delegation ends on its last day, or when the delegator (or a platform admin) revokes it. It is
  never deleted.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from close_app import audit
from close_app.authz import GLOBAL, Action, holds
from close_app.errors import Denied, Problem
from close_app.guard import deny
from close_app.identity import Principal

FIELDS: Final = (
    "delegation_id", "delegator", "delegate", "entity_ids", "starts_on", "ends_on", "created_by", "created_at",
    "revoked_by", "revoked_at",
)  # fmt: skip
_INSERT: Final = text(
    "INSERT INTO delegations (delegation_id, delegator, delegate, entity_ids, starts_on, ends_on, created_by) "
    "VALUES (:id, :from, :to, :e, :s, :end, :by)"
)
_ONE_LOCKED: Final = text("SELECT * FROM delegations WHERE delegation_id = :id FOR UPDATE")
_MINE: Final = text(
    "SELECT * FROM delegations WHERE delegator = :u OR delegate = :u ORDER BY created_at DESC, delegation_id"
)
_SEES: Final = text(
    "SELECT DISTINCT scope FROM role_assignments WHERE user_id = :u AND revoked_at IS NULL AND scope = ANY (:e)"
)
_REVOKE: Final = text(
    "UPDATE delegations SET revoked_by = :by, revoked_at = clock_timestamp() WHERE delegation_id = :id"
)


def as_json(row: RowMapping) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in FIELDS:
        value = row[name]
        out[name] = value.isoformat() if isinstance(value, date | datetime) else value
    out["delegation_id"], out["entity_ids"] = str(row["delegation_id"]), list(row["entity_ids"])
    return out


def _audit(tx: Connection, actor: str, verb: str, row: RowMapping, request_id: str | None) -> None:
    event = audit.Event(
        actor,
        f"delegation.{verb}",
        entity_ids=tuple(row["entity_ids"]),
        target=f"delegation:{row['delegation_id']}",
        payload={"delegator": row["delegator"], "delegate": row["delegate"], "ends_on": row["ends_on"].isoformat()},
    )
    audit.append(tx, event, request_id)


def create(
    tx: Connection,
    p: Principal,
    *,
    delegate: str,
    entity_ids: list[str],
    starts_on: date,
    ends_on: date,
    request_id: str | None,
) -> RowMapping:
    entities = sorted(set(entity_ids))
    for entity_id in entities:
        if not holds(p.assignments, Action.JOURNAL_DELEGATE, entity_id):
            raise deny(p, Action.JOURNAL_DELEGATE, f"delegation:{p.user_id}:{delegate}", [entity_id])
    if delegate == p.user_id:
        raise Denied(
            p.user_id,
            Action.JOURNAL_DELEGATE.value,
            target=f"delegation:{p.user_id}:{delegate}",
            entity_ids=tuple(entities),
            sod="nobody can delegate to themselves",
        )
    if ends_on < starts_on:
        raise Problem(422, "The delegation ends before it starts", kind="validation-failed")
    sees = {str(s) for (s,) in tx.execute(_SEES, {"u": delegate, "e": entities})}
    if blind := [e for e in entities if e not in sees]:
        detail = f"{delegate} holds no role in {', '.join(blind)}, so they couldn't see its journals"
        raise Problem(422, "The delegate can't see those entities", detail, kind="validation-failed")
    delegation_id = uuid.uuid4()
    tx.execute(
        _INSERT,
        {
            "id": delegation_id,
            "from": p.user_id,
            "to": delegate,
            "e": entities,
            "s": starts_on,
            "end": ends_on,
            "by": p.user_id,
        },
    )
    row = tx.execute(_ONE_LOCKED, {"id": delegation_id}).mappings().one()
    _audit(tx, p.user_id, "create", row, request_id)
    return row


def load_locked(tx: Connection, delegation_id: UUID) -> RowMapping | None:
    return tx.execute(_ONE_LOCKED, {"id": delegation_id}).mappings().first()


def revoke(tx: Connection, row: RowMapping, p: Principal, request_id: str | None) -> RowMapping:
    admin = holds(p.assignments, Action.ACCESS_GRANT, GLOBAL)
    if p.user_id != row["delegator"] and not admin:
        raise deny(p, Action.JOURNAL_DELEGATE, f"delegation:{row['delegation_id']}", list(row["entity_ids"]))
    if row["revoked_at"] is not None:
        raise Problem(409, "The delegation is already revoked", kind="conflict")
    tx.execute(_REVOKE, {"id": row["delegation_id"], "by": p.user_id})
    new = tx.execute(_ONE_LOCKED, {"id": row["delegation_id"]}).mappings().one()
    _audit(tx, p.user_id, "revoke", new, request_id)
    return new


def mine(conn: Connection, user_id: str) -> list[dict[str, Any]]:
    return [as_json(r) for r in conn.execute(_MINE, {"u": user_id}).mappings()]


def new_id() -> UUID:
    return uuid.uuid4()
