"""Accounting periods, authoritative in Postgres (D6): each entity's periods move ``open -> closed -> locked``, and
from Stage 8b ``open -> soft_closed -> closed`` (ADR-0020).

- **Who.** Only a holder of ``period.manage`` in the entity (the controller) opens, soft-closes, closes, locks or
  reopens.
- **A soft close** stops automated entries (recurring journals, automatic reversals, the journaling engine) and
  keeps manual ones, for the close review's adjustments (``store.TAKES_MANUAL``, ``store.TAKES_AUTOMATED``). Like a
  close, it is refused while a journaling run is in progress, because the run's snapshot read the statuses.
- **Closing** locks the period row ``FOR UPDATE`` and is refused while any journal dated in it waits for approval.
  Journal moves hold the row ``FOR SHARE``, so the two can't interleave. A period with a close checklist closes only
  from ``soft_closed``, once every task is done and no GL check is stale (``close.gate``).
- **Reopening** needs a reason. A locked period can't be reopened.
- **Opening** a period also creates the automatic reversals due in it, for approved journals of the previous
  period whose template reverses automatically.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import Connection, RowMapping, text

from close_app import audit
from close_app.close import gate
from close_app.errors import Problem
from close_app.journals import store, workflow
from close_app.journals.setup import Setup

FIELDS: Final = (
    "entity_id", "period", "starts_on", "ends_on", "status", "opened_by", "opened_at", "soft_closed_by",
    "soft_closed_at", "closed_by", "closed_at", "locked_by", "locked_at", "reopened_by", "reopened_at",
    "reopen_reason", "row_version",
)  # fmt: skip
_ONE: Final = text("SELECT * FROM accounting_periods WHERE entity_id = :e AND period = :p")
_ONE_LOCKED: Final = text("SELECT * FROM accounting_periods WHERE entity_id = :e AND period = :p FOR UPDATE")
_INSERT: Final = text(
    "INSERT INTO accounting_periods (entity_id, period, starts_on, ends_on, status, opened_by) "
    "VALUES (:e, :p, :s, :end, 'open', :by) ON CONFLICT DO NOTHING RETURNING period"
)
_WAITING: Final = text(  # waiting for approval, or approved but not yet in the ledger (Stage 7b)
    "SELECT number FROM journals WHERE entity_id = :e AND period = :p "
    "AND status IN ('pending_approval', 'approved', 'posting') ORDER BY number"
)
_MOVES: Final = {
    "soft-close": text(
        "UPDATE accounting_periods SET status = 'soft_closed', soft_closed_by = :by, "
        "soft_closed_at = clock_timestamp(), row_version = row_version + 1 WHERE entity_id = :e AND period = :p"
    ),
    "close": text(
        "UPDATE accounting_periods SET status = 'closed', closed_by = :by, closed_at = clock_timestamp(), "
        "row_version = row_version + 1 WHERE entity_id = :e AND period = :p"
    ),
    "lock": text(
        "UPDATE accounting_periods SET status = 'locked', locked_by = :by, locked_at = clock_timestamp(), "
        "row_version = row_version + 1 WHERE entity_id = :e AND period = :p"
    ),
    "reopen": text(
        "UPDATE accounting_periods SET status = 'open', reopened_by = :by, reopened_at = clock_timestamp(), "
        "reopen_reason = :why, row_version = row_version + 1 WHERE entity_id = :e AND period = :p"
    ),
}
FROM_STATUS: Final = {
    "soft-close": ("open",),
    "close": ("open", "soft_closed"),
    "lock": ("closed",),
    "reopen": ("soft_closed", "closed"),
}
_JOURNALING: Final = text("SELECT 1 FROM journaling_runs WHERE entity_id = :e AND status = 'running' LIMIT 1")
_DUE_REVERSALS: Final = text(  # approved, or posted since (Stage 7b): either way its reversal is due
    "SELECT * FROM journals WHERE entity_id = :e AND period = :p AND status = ANY (:done) AND auto_reverse "
    "ORDER BY number"
)


def _no_pause() -> None:
    return None


# Tests pause a close here, after its check and before its update, to prove the row locks keep journals out.
pause_after_close_check: Callable[[], None] = _no_pause


def bounds(period: str) -> tuple[date, date]:
    year, month = int(period[:4]), int(period[5:])
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def previous(period: str) -> str:
    year, month = int(period[:4]), int(period[5:])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def etag(row: RowMapping) -> str:
    return f'"{row["row_version"]}-{row["entity_id"]}-{row["period"]}"'


def target(row: RowMapping | dict[str, Any]) -> str:
    return f"period:{row['entity_id']}:{row['period']}"


def as_json(row: RowMapping) -> dict[str, Any]:
    out = {k: (v.isoformat() if isinstance(v, date | datetime) else v) for k in FIELDS for v in [row[k]]}
    out["etag"] = etag(row)
    return out


def load(tx: Connection, entity_id: str, period: str, *, lock: bool = False) -> RowMapping | None:
    return tx.execute(_ONE_LOCKED if lock else _ONE, {"e": entity_id, "p": period}).mappings().first()


def _audit(tx: Connection, actor: str, verb: str, row: RowMapping, request_id: str | None, **payload: Any) -> None:
    event = audit.Event(
        actor,
        f"period.{verb}",
        entity_ids=(row["entity_id"],),
        target=target(row),
        payload={"status": row["status"], "row_version": row["row_version"], **payload},
    )
    audit.append(tx, event, request_id)


def open_new(
    tx: Connection, setup: Setup, entity_id: str, period: str, *, actor: str, request_id: str | None
) -> RowMapping:
    setup.entity(entity_id)  # a 422 for an entity outside the entity set
    starts, ends = bounds(period)
    created = tx.execute(_INSERT, {"e": entity_id, "p": period, "s": starts, "end": ends, "by": actor}).first()
    if created is None:
        raise Problem(409, "The period already exists", f"{entity_id} {period}", kind="conflict")
    row = load(tx, entity_id, period)
    if row is None:
        raise RuntimeError("the period just opened is missing")
    _audit(tx, actor, "open", row, request_id)
    due = {"e": entity_id, "p": previous(period), "done": list(store.APPROVED_OR_LATER)}
    for journal in tx.execute(_DUE_REVERSALS, due).mappings().all():
        workflow.auto_reverse(tx, setup, journal, request_id)
    return row


def move(
    tx: Connection, row: RowMapping, verb: str, *, actor: str, reason: str | None, request_id: str | None
) -> RowMapping:
    """Soft-close, close, lock or reopen a period whose row the caller has locked ``FOR UPDATE``."""
    wanted = FROM_STATUS[verb]
    if row["status"] not in wanted:
        detail = (
            "a locked period can't be reopened"
            if row["status"] == "locked"
            else f"{verb} needs a {' or '.join(wanted)} period"
        )
        raise Problem(409, f"Cannot {verb} a {row['status']} period", detail, kind="conflict")
    if verb in ("soft-close", "close") and tx.execute(_JOURNALING, {"e": row["entity_id"]}).first() is not None:
        raise Problem(  # Stage 8a: its snapshot holds the period statuses it started with
            409, "A journaling run is in progress", f"for {row['entity_id']}; {verb} when it ends", kind="conflict"
        )
    if verb == "close":
        waiting = [int(n) for (n,) in tx.execute(_WAITING, {"e": row["entity_id"], "p": row["period"]})]
        if waiting:
            listed = ", ".join(f"{row['entity_id']}-{n}" for n in waiting[:20])
            raise Problem(409, "Journals in the period are waiting for approval or posting", listed, kind="conflict")
        if gate.has_checklist(tx, row["entity_id"], row["period"]):  # Stage 8b: PeriodCloseWorkflow
            if row["status"] != "soft_closed":
                detail = "a period with a close checklist is soft-closed first"
                raise Problem(409, "Soft-close the period first", detail, kind="conflict")
            blocked = gate.blockers(tx, row["entity_id"], row["period"])
            if blocked:
                raise Problem(409, "The close checklist isn't done", "; ".join(blocked[:20]), kind="conflict")
        pause_after_close_check()
    if verb == "reopen" and not reason:
        raise Problem(422, "Reopening a period needs a reason", kind="validation-failed")
    tx.execute(_MOVES[verb], {"e": row["entity_id"], "p": row["period"], "by": actor, "why": reason})
    new = load(tx, row["entity_id"], row["period"])
    if new is None:
        raise RuntimeError("the period disappeared")
    _audit(tx, actor, verb, new, request_id, **({"reason": reason} if reason else {}))
    return new
