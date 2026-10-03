"""Journal batches (BlackLine's Journal Batching): submitted, approved and rejected as one, never partly.

- **Members** are drafts of the batch's entity, added by their preparer while the batch is a draft.
- **Routing.** Each member is routed as a journal would be, and the batch needs the most levels any member
  needs. Every member waits at the same level.
- **Separation of duties** covers the batch's creator and every member's preparer. One person approves at most
  one level of the batch.
- **Locks.** Members are locked in id order, then their periods ``FOR SHARE``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from close_app import audit
from close_app.errors import Denied, Problem
from close_app.identity import Principal
from close_app.journals import store, workflow
from close_app.journals.setup import Setup
from close_contracts.fx import Rate

FIELDS: Final = (
    "batch_id", "entity_id", "description", "status", "created_by", "created_at", "submission", "submitted_by",
    "submitted_at", "required_levels", "current_level", "level_since", "approved_at", "cancelled_by", "cancelled_at",
    "row_version",
)  # fmt: skip
_ONE: Final = text("SELECT * FROM journal_batches WHERE batch_id = :id")
_ONE_LOCKED: Final = text("SELECT * FROM journal_batches WHERE batch_id = :id FOR UPDATE")
_INSERT: Final = text(
    "INSERT INTO journal_batches (batch_id, entity_id, description, status, created_by) "
    "VALUES (:id, :e, :d, 'draft', :by)"
)
_MEMBERS: Final = text("SELECT * FROM journals WHERE batch_id = :id ORDER BY number")
_MEMBERS_LOCKED: Final = text("SELECT * FROM journals WHERE batch_id = :id ORDER BY journal_id FOR UPDATE")
_SET_BATCH: Final = text("UPDATE journals SET batch_id = :b, row_version = row_version + 1 WHERE journal_id = :id")
_UNSET_ALL: Final = text("UPDATE journals SET batch_id = NULL, row_version = row_version + 1 WHERE batch_id = :b")
_SUBMITTED: Final = text(
    "UPDATE journal_batches SET status = 'pending_approval', submission = submission + 1, submitted_by = :by, "
    "submitted_at = :at, required_levels = :lv, current_level = 1, level_since = :at, row_version = row_version + 1 "
    "WHERE batch_id = :id"
)
_NEXT_LEVEL: Final = text(
    "UPDATE journal_batches SET current_level = current_level + 1, level_since = :at, row_version = row_version + 1 "
    "WHERE batch_id = :id"
)
_APPROVED: Final = text(
    "UPDATE journal_batches SET status = 'approved', approved_at = :at, current_level = NULL, level_since = NULL, "
    "row_version = row_version + 1 WHERE batch_id = :id"
)
_BACK: Final = text(
    "UPDATE journal_batches SET status = 'draft', current_level = NULL, level_since = NULL, "
    "row_version = row_version + 1 WHERE batch_id = :id"
)
_CANCELLED: Final = text(
    "UPDATE journal_batches SET status = 'cancelled', cancelled_by = :by, cancelled_at = clock_timestamp(), "
    "row_version = row_version + 1 WHERE batch_id = :id"
)


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date | datetime) else str(value) if isinstance(value, UUID) else value


def load(tx: Connection, batch_id: str | UUID, *, lock: bool = False) -> RowMapping | None:
    try:
        key = UUID(str(batch_id))
    except ValueError:
        return None
    return tx.execute(_ONE_LOCKED if lock else _ONE, {"id": key}).mappings().first()


def _reload(tx: Connection, row: RowMapping) -> RowMapping:
    new = load(tx, row["batch_id"])
    if new is None:
        raise RuntimeError("the batch disappeared")
    return new


def members(tx: Connection, row: RowMapping, *, lock: bool = False) -> list[RowMapping]:
    return list(tx.execute(_MEMBERS_LOCKED if lock else _MEMBERS, {"id": row["batch_id"]}).mappings())


def etag(row: RowMapping) -> str:
    return f'"{row["row_version"]}-{str(row["batch_id"])[:8]}"'


def target(row: RowMapping) -> str:
    return f"journal_batch:{row['entity_id']}:{row['batch_id']}"


def as_json(tx: Connection, row: RowMapping) -> dict[str, Any]:
    out = {name: _plain(row[name]) for name in FIELDS}
    out["etag"] = etag(row)
    out["journals"] = [store.as_json(tx, m, detail=False) for m in members(tx, row)]
    return out


def _audit(tx: Connection, actor: str, verb: str, row: RowMapping, request_id: str | None, **payload: Any) -> None:
    event = audit.Event(
        actor,
        f"journal_batch.{verb}",
        entity_ids=(row["entity_id"],),
        target=target(row),
        payload={"batch_id": str(row["batch_id"]), "status": row["status"], **payload},
    )
    audit.append(tx, event, request_id)


def _need(row: RowMapping, status: str, verb: str) -> None:
    if row["status"] != status:
        raise Problem(409, f"Cannot {verb} a {row['status']} batch", f"{verb} needs a {status} batch", kind="conflict")


def _creator_only(p: Principal, row: RowMapping, verb: str) -> None:
    if p.user_id != row["created_by"]:
        raise Denied(
            p.user_id,
            "journal.prepare",
            target=target(row),
            entity_ids=(row["entity_id"],),
            sod=f"only the batch's creator can {verb} it",
        )


def create(tx: Connection, entity_id: str, description: str, *, p: Principal, request_id: str | None) -> RowMapping:
    batch_id = uuid.uuid4()
    tx.execute(_INSERT, {"id": batch_id, "e": entity_id, "d": description, "by": p.user_id})
    row = load(tx, batch_id)
    if row is None:
        raise RuntimeError("the batch just created is missing")
    store.history(tx, action="batch_create", actor=p.user_id, batch_id=batch_id, to_status="draft")
    _audit(tx, p.user_id, "create", row, request_id)
    return row


def add(tx: Connection, row: RowMapping, journal: RowMapping, *, p: Principal, request_id: str | None) -> RowMapping:
    _need(row, "draft", "add to")
    workflow.need_status(journal, "draft", verb="add to a batch")
    workflow.preparer_only(p, journal, "add it to a batch")
    if journal["entity_id"] != row["entity_id"]:
        raise Problem(422, "A batch holds journals of one entity", kind="validation-failed")
    if journal["batch_id"] is not None:
        raise Problem(409, "The journal is already in a batch", str(journal["batch_id"]), kind="conflict")
    tx.execute(_SET_BATCH, {"b": row["batch_id"], "id": journal["journal_id"]})
    store.history(tx, action="batch_add", actor=p.user_id, journal=journal, batch_id=row["batch_id"])
    _audit(tx, p.user_id, "add", row, request_id, journal_id=str(journal["journal_id"]))
    return _reload(tx, row)


def remove(tx: Connection, row: RowMapping, journal: RowMapping, *, p: Principal, request_id: str | None) -> RowMapping:
    _need(row, "draft", "remove from")
    if journal["batch_id"] != row["batch_id"]:
        raise Problem(409, "The journal isn't in this batch", kind="conflict")
    workflow.preparer_only(p, journal, "remove it from a batch")
    tx.execute(_SET_BATCH, {"b": None, "id": journal["journal_id"]})
    store.history(tx, action="batch_remove", actor=p.user_id, journal=journal, batch_id=row["batch_id"])
    _audit(tx, p.user_id, "remove", row, request_id, journal_id=str(journal["journal_id"]))
    return _reload(tx, row)


def _locked_members(tx: Connection, row: RowMapping) -> list[RowMapping]:
    found = members(tx, row, lock=True)
    for m in found:
        workflow.open_period(tx, m["entity_id"], m["period"])
    return found


def submittable(p: Principal, row: RowMapping) -> None:
    """The checks a batch submit makes before its members' rates are looked up (and again under the lock)."""
    _need(row, "draft", "submit")
    _creator_only(p, row, "submit")


def submit(
    tx: Connection,
    setup: Setup,
    row: RowMapping,
    *,
    p: Principal,
    request_id: str | None,
    as_of: datetime | None = None,
    rates: Mapping[UUID, Rate] | None = None,
) -> RowMapping:
    """Submit every member together. ``rates`` holds each foreign-currency member's rate, looked up by the caller
    before the transaction; one missing rate refuses the whole batch."""
    _need(row, "draft", "submit")
    _creator_only(p, row, "submit")
    found = _locked_members(tx, row)
    if not found:
        raise Problem(409, "The batch is empty", "add journals first", kind="conflict")
    for m in found:
        workflow.need_status(m, "draft", verb="submit")
    at = as_of or workflow.now(tx)
    frozen = []
    for m in found:
        amounts = workflow.frozen_amounts(tx, setup, m, (rates or {}).get(m["journal_id"]))
        frozen.append((m, amounts, workflow.routed(tx, setup, m, at, amounts)))
    levels = max(int(str(f["lv"])) for _, _, f in frozen)
    for m, amounts, f in frozen:
        workflow.mark_submitted(
            tx, m, f, actor=p.user_id, at=at, request_id=request_id, levels=levels, conversion=amounts.conversion
        )
    tx.execute(_SUBMITTED, {"id": row["batch_id"], "by": p.user_id, "at": at, "lv": levels})
    new = _reload(tx, row)
    store.history(
        tx,
        action="batch_submit",
        actor=p.user_id,
        batch_id=row["batch_id"],
        from_status="draft",
        to_status="pending_approval",
        submission=new["submission"],
    )
    _audit(tx, p.user_id, "submit", new, request_id, required_levels=levels, journals=len(found))
    return new


def _preparers(row: RowMapping, found: list[RowMapping]) -> set[str]:
    return {row["created_by"]} | {m["created_by"] for m in found}


def approve(
    tx: Connection,
    setup: Setup,
    row: RowMapping,
    *,
    p: Principal,
    request_id: str | None,
    as_of: datetime | None = None,
) -> RowMapping:
    _need(row, "pending_approval", "approve")
    found = _locked_members(tx, row)
    at = as_of or workflow.now(tx)
    level = int(row["current_level"])
    who = workflow.approver_for(
        tx,
        p,
        entity_id=row["entity_id"],
        level=level,
        preparers=_preparers(row, found),
        target=target(row),
        verb="approve",
        at=at,
    )
    for m in found:
        workflow.one_level_each(tx, p, m, who, level)
    for m in found:
        workflow.record_approval(tx, m, who, level=level, at=at, batch_id=row["batch_id"])
        workflow.advance(tx, setup, m, who, level=level, at=at, request_id=request_id)
    final = level >= row["required_levels"]
    tx.execute(_APPROVED if final else _NEXT_LEVEL, {"id": row["batch_id"], "at": at})
    new = _reload(tx, row)
    store.history(
        tx,
        action="batch_approve",
        actor=who.approver,
        on_behalf_of=who.on_behalf_of,
        batch_id=row["batch_id"],
        from_status="pending_approval",
        to_status=new["status"],
        submission=new["submission"],
        level=level,
    )
    _audit(tx, who.approver, "approve", new, request_id, level=level, on_behalf_of=who.on_behalf_of)
    return new


def _back(
    tx: Connection,
    row: RowMapping,
    found: list[RowMapping],
    *,
    action: str,
    actor: str,
    on_behalf_of: str | None,
    reason: str | None,
    request_id: str | None,
) -> RowMapping:
    for m in found:
        workflow.back_to_draft(
            tx, m, action=action, actor=actor, on_behalf_of=on_behalf_of, reason=reason, request_id=request_id
        )
    tx.execute(_BACK, {"id": row["batch_id"]})
    new = _reload(tx, row)
    store.history(
        tx,
        action=f"batch_{action}",
        actor=actor,
        on_behalf_of=on_behalf_of,
        batch_id=row["batch_id"],
        from_status="pending_approval",
        to_status="draft",
        submission=new["submission"],
        level=row["current_level"],
        reason=reason,
    )
    _audit(tx, actor, action, new, request_id, reason=reason)
    return new


def reject(
    tx: Connection,
    row: RowMapping,
    *,
    p: Principal,
    reason: str,
    request_id: str | None,
    as_of: datetime | None = None,
) -> RowMapping:
    _need(row, "pending_approval", "reject")
    found = members(tx, row, lock=True)
    at = as_of or workflow.now(tx)
    who = workflow.approver_for(
        tx,
        p,
        entity_id=row["entity_id"],
        level=int(row["current_level"]),
        preparers=_preparers(row, found),
        target=target(row),
        verb="reject",
        at=at,
    )
    return _back(
        tx,
        row,
        found,
        action="reject",
        actor=who.approver,
        on_behalf_of=who.on_behalf_of,
        reason=reason,
        request_id=request_id,
    )


def recall(tx: Connection, row: RowMapping, *, p: Principal, request_id: str | None) -> RowMapping:
    _need(row, "pending_approval", "recall")
    _creator_only(p, row, "recall")
    found = members(tx, row, lock=True)
    return _back(
        tx, row, found, action="recall", actor=p.user_id, on_behalf_of=None, reason=None, request_id=request_id
    )


def cancel(tx: Connection, row: RowMapping, *, p: Principal, request_id: str | None) -> RowMapping:
    """Cancel a draft batch. Its journals stay, as drafts outside any batch."""
    _need(row, "draft", "cancel")
    _creator_only(p, row, "cancel")
    members(tx, row, lock=True)
    tx.execute(_UNSET_ALL, {"b": row["batch_id"]})
    tx.execute(_CANCELLED, {"id": row["batch_id"], "by": p.user_id})
    new = _reload(tx, row)
    store.history(
        tx, action="batch_cancel", actor=p.user_id, batch_id=row["batch_id"], from_status="draft", to_status="cancelled"
    )
    _audit(tx, p.user_id, "cancel", new, request_id)
    return new
