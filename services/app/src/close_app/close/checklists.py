"""A period's close checklist and its tasks (Stage 8b, ADR-0020): a Postgres state machine with idempotent tasks,
``PeriodCloseWorkflow`` without Temporal (ADR-0007).

- **Generating** (:func:`plan`, then :func:`generate`). The controller generates an entity's checklist for a period
  from the active close checklist. Outside the transaction, the tasks' due dates are worked out from the entity's
  business calendar (the narrow lookup). Inside it, the checklist version is checked again and the tasks are copied
  and frozen, so a later version never changes a close in progress.
- **A task** moves ``open -> prepared -> done``. A task without a reviewer is done when prepared.
  - **A manual task** is prepared by its preparer, only once the tasks it depends on are done.
  - **An automatic task** is completed by its check (:func:`record_check`), which a user runs. A failing check
    leaves it open, and a passing one that is run again is recorded again.
  - **Reviewing.** Its reviewer reviews it or sends it back. A reviewer never reviews their own preparation, which
    the table's constraints also enforce.
  - **The controller** reassigns a task that isn't done, or reopens one with a reason.
  - **Assignment.** Acting on a task assigned to someone else is a 409, never a 403, because the role allows it.
- **Periods.** Tasks move only while their period takes entries (open or soft-closed), read ``FOR SHARE``, so a
  close can't interleave with a task move.
- **History.** Every move adds a row to the insert-only ``close_task_events`` and an audit event.
- **Reminders** (:func:`run_reminders`) read Postgres only. They remind whoever a due task waits on once a day, and
  tell the controller once a day when the task is overdue by the checklist's days.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import Connection, Engine, RowMapping, text

from close_app import audit, close_configs
from close_app.authz import Action
from close_app.errors import Problem
from close_app.identity import Principal
from close_app.journals import periods, store, workflow
from close_app.journals.tasks import holders, notify
from close_contracts.config.close import CloseChecklist, CloseTask, calendar_window, due_date, topological
from close_platform.query.calendars import CalendarLookup

TASK_FIELDS: Final = (
    "task_id", "position", "name", "instructions", "due_day", "due_on", "depends_on", "preparer", "reviewer",
    "check_name", "status", "prepared_by", "prepared_at", "note", "reviewed_by", "reviewed_at", "checked_at",
    "evidence", "row_version",
)  # fmt: skip
_CHECKLIST: Final = text("SELECT * FROM close_checklists WHERE entity_id = :e AND period = :p")
_TASKS: Final = text("SELECT * FROM close_tasks WHERE entity_id = :e AND period = :p ORDER BY position")
_TASK: Final = text("SELECT * FROM close_tasks WHERE entity_id = :e AND period = :p AND task_id = :t")
_TASK_LOCKED: Final = text("SELECT * FROM close_tasks WHERE entity_id = :e AND period = :p AND task_id = :t FOR UPDATE")
_PERIOD_SHARE: Final = text("SELECT * FROM accounting_periods WHERE entity_id = :e AND period = :p FOR SHARE")
_INSERT_CHECKLIST: Final = text(
    "INSERT INTO close_checklists (entity_id, period, checklist_id, checklist_version, calendar_code, "
    "calendar_version, escalate_after_days, generated_by) VALUES (:e, :p, :c, :v, :cal, :calv, :esc, :by) "
    "ON CONFLICT (entity_id, period) DO NOTHING RETURNING period"
)
_INSERT_TASK: Final = text(
    "INSERT INTO close_tasks (entity_id, period, task_id, position, name, instructions, due_day, due_on, depends_on, "
    "preparer, reviewer, check_name, status) VALUES (:e, :p, :t, :pos, :name, :ins, :day, :due, :deps, :prep, :rev, "
    ":check, 'open')"
)
_EVENT: Final = text(
    "INSERT INTO close_task_events (event_id, entity_id, period, task_id, action, actor, note, detail) "
    "VALUES (:id, :e, :p, :t, :a, :by, :note, CAST(:detail AS jsonb))"
)
_PREPARED: Final = text(
    "UPDATE close_tasks SET status = :st, prepared_by = :by, prepared_at = clock_timestamp(), note = :note, "
    "reviewed_by = NULL, reviewed_at = NULL, checked_at = :checked, evidence = CAST(:evidence AS jsonb), "
    "row_version = row_version + 1 WHERE entity_id = :e AND period = :p AND task_id = :t"
)
_REVIEWED: Final = text(
    "UPDATE close_tasks SET status = 'done', reviewed_by = :by, reviewed_at = clock_timestamp(), "
    "row_version = row_version + 1 WHERE entity_id = :e AND period = :p AND task_id = :t"
)
_REOPENED: Final = text(
    "UPDATE close_tasks SET status = 'open', prepared_by = NULL, prepared_at = NULL, reviewed_by = NULL, "
    "reviewed_at = NULL, checked_at = NULL, evidence = CAST(:evidence AS jsonb), row_version = row_version + 1 "
    "WHERE entity_id = :e AND period = :p AND task_id = :t"
)
_REASSIGNED: Final = text(
    "UPDATE close_tasks SET preparer = :prep, reviewer = :rev, row_version = row_version + 1 "
    "WHERE entity_id = :e AND period = :p AND task_id = :t"
)
_DUE: Final = text(
    "SELECT t.*, c.escalate_after_days FROM close_tasks t "
    "JOIN close_checklists c ON c.entity_id = t.entity_id AND c.period = t.period "
    "JOIN accounting_periods a ON a.entity_id = t.entity_id AND a.period = t.period "
    "WHERE t.status <> 'done' AND t.due_on <= :today AND a.status IN ('open', 'soft_closed') "
    "ORDER BY t.entity_id, t.period, t.position"
)


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date | datetime) else value


def task_json(row: RowMapping) -> dict[str, Any]:
    out = {k: _plain(row[k]) for k in TASK_FIELDS}
    out["depends_on"] = list(row["depends_on"])
    out["kind"] = "automatic" if row["check_name"] else "manual"
    return out


def target(entity_id: str, period: str, task_id: str | None = None) -> str:
    return f"close_task:{entity_id}:{period}:{task_id}" if task_id else f"close_checklist:{entity_id}:{period}"


def checklist_json(tx: Connection, entity_id: str, period: str) -> dict[str, Any]:
    head = tx.execute(_CHECKLIST, {"e": entity_id, "p": period}).mappings().first()
    if head is None:
        raise Problem(404, "Not found", f"no checklist has been generated for {entity_id} {period}", kind="not-found")
    tasks = [task_json(r) for r in tx.execute(_TASKS, {"e": entity_id, "p": period}).mappings()]
    return {
        "entity_id": entity_id,
        "period": period,
        "checklist_id": head["checklist_id"],
        "checklist_version": head["checklist_version"],
        "calendar_code": head["calendar_code"],
        "calendar_version": head["calendar_version"],
        "escalate_after_days": head["escalate_after_days"],
        "generated_by": head["generated_by"],
        "generated_at": head["generated_at"].isoformat(),
        "tasks": tasks,
        "done": sum(1 for t in tasks if t["status"] == "done"),
        "complete": all(t["status"] == "done" for t in tasks),
    }


# ---------------------------------------------------------------------------------------------- generating


@dataclass(frozen=True)
class Plan:
    """What generation works out before its transaction."""

    checklist: CloseChecklist
    checklist_id: str
    version: int
    tasks: tuple[CloseTask, ...]  # the entity's, each after the tasks it depends on
    due: dict[str, date]
    calendar_code: str
    calendar_version: int | None


def plan(engine: Engine, calendars: CalendarLookup | None, entity_id: str, period: str) -> Plan:
    """The active checklist's tasks for the entity, and their due dates from the entity's business calendar."""
    with engine.connect() as conn:
        found = close_configs.active_checklist(conn)
    if found is None:
        raise Problem(409, "No close checklist is active", kind="conflict")
    checklist, checklist_id, version = found
    mine = {t.task_id: t for t in checklist.tasks_for(entity_id)}
    if not mine:
        raise Problem(409, "The close checklist has no tasks for the entity", entity_id, kind="conflict")
    ordered = tuple(mine[t] for t in topological(list(mine.values())))
    if calendars is None:
        raise Problem(503, "Business calendars aren't available", "the read path isn't configured")
    _, ends = periods.bounds(period)
    start, end = calendar_window(ends, [t.due_day for t in ordered])
    calendar = calendars.calendar(entity_id, start, end)
    if calendar is None:
        detail = f"ref.entities has no calendar code for {entity_id}"
        raise Problem(422, "The entity has no business calendar", detail, kind="validation-failed")
    due = {t.task_id: due_date(ends, t.due_day, calendar.days) for t in ordered}
    missing = [t for t, d in due.items() if d is None]
    if missing:
        detail = f"{calendar.calendar_code} has no days for the due dates of: {', '.join(missing)}"
        raise Problem(422, "The calendar doesn't cover the due dates", detail, kind="validation-failed")
    return Plan(
        checklist,
        checklist_id,
        version,
        ordered,
        {t: d for t, d in due.items() if d is not None},
        calendar.calendar_code,
        calendar.version,
    )


def _period(tx: Connection, entity_id: str, period: str) -> RowMapping:
    """The period, held ``FOR SHARE``: it must exist and still take entries (open or soft-closed)."""
    row = tx.execute(_PERIOD_SHARE, {"e": entity_id, "p": period}).mappings().first()
    if row is None:
        raise Problem(404, "Not found", f"period {entity_id} {period}", kind="not-found")
    if row["status"] not in store.TAKES_MANUAL:
        raise Problem(409, f"The period is {row['status']}", f"{entity_id} {period}", kind="conflict")
    return row


def _event(
    tx: Connection,
    row: RowMapping | dict[str, Any],
    action: str,
    actor: str,
    *,
    note: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    tx.execute(
        _EVENT,
        {
            "id": uuid.uuid4(),
            "e": row["entity_id"],
            "p": row["period"],
            "t": row["task_id"],
            "a": action,
            "by": actor,
            "note": note,
            "detail": json.dumps(detail, default=str) if detail is not None else None,
        },
    )


def _audit(tx: Connection, actor: str, action: str, row: RowMapping, request_id: str | None, **payload: Any) -> None:
    event = audit.Event(
        actor,
        f"close.task.{action}",
        entity_ids=(row["entity_id"],),
        target=target(row["entity_id"], row["period"], row["task_id"]),
        payload={"status": row["status"], "row_version": row["row_version"], **payload},
    )
    audit.append(tx, event, request_id)


def generate(
    tx: Connection, made: Plan, entity_id: str, period: str, *, p: Principal, request_id: str | None
) -> dict[str, Any]:
    _period(tx, entity_id, period)
    current = close_configs.active_checklist(tx)
    if current is None or (current[1], current[2]) != (made.checklist_id, made.version):
        detail = "the active close checklist changed while the due dates were worked out: generate it again"
        raise Problem(409, "The close checklist changed", detail, kind="conflict")
    created = tx.execute(
        _INSERT_CHECKLIST,
        {
            "e": entity_id,
            "p": period,
            "c": made.checklist_id,
            "v": made.version,
            "cal": made.calendar_code,
            "calv": made.calendar_version,
            "esc": made.checklist.reminders.escalate_after_days,
            "by": p.user_id,
        },
    ).first()
    if created is None:  # generated already, or by a concurrent request that committed first
        raise Problem(409, "The period already has a checklist", f"{entity_id} {period}", kind="conflict")
    for position, task in enumerate(made.tasks, start=1):
        values = {
            "e": entity_id,
            "p": period,
            "t": task.task_id,
            "pos": position,
            "name": task.name,
            "ins": task.instructions,
            "day": task.due_day,
            "due": made.due[task.task_id],
            "deps": list(task.depends_on),
            "prep": task.preparer,
            "rev": task.reviewer,
            "check": task.check,
        }
        tx.execute(_INSERT_TASK, values)
        _event(tx, {"entity_id": entity_id, "period": period, "task_id": task.task_id}, "generate", p.user_id)
    audit.append(
        tx,
        audit.Event(
            p.user_id,
            "close.checklist.generate",
            entity_ids=(entity_id,),
            target=target(entity_id, period),
            payload={
                "checklist_id": made.checklist_id,
                "version": made.version,
                "tasks": len(made.tasks),
                "calendar": made.calendar_code,
                "calendar_version": made.calendar_version,
            },
        ),
        request_id,
    )
    return checklist_json(tx, entity_id, period)


# ---------------------------------------------------------------------------------------------- task moves


def task(tx: Connection, entity_id: str, period: str, task_id: str, *, lock: bool = False) -> RowMapping:
    row = tx.execute(_TASK_LOCKED if lock else _TASK, {"e": entity_id, "p": period, "t": task_id}).mappings().first()
    if row is None:
        raise Problem(404, "Not found", f"close task {entity_id} {period} {task_id}", kind="not-found")
    return row


def _reload(tx: Connection, row: RowMapping) -> RowMapping:
    return task(tx, row["entity_id"], row["period"], row["task_id"])


def _conflict(title: str, detail: str | None = None) -> Problem:
    return Problem(409, title, detail, kind="conflict")


def _assigned(row: RowMapping, who: str, user_id: str) -> None:
    if row[who] != user_id:
        raise _conflict(f"The task's {who} is someone else", f"{row['task_id']} is assigned to {row[who]}")


def _dependencies_done(tx: Connection, row: RowMapping) -> None:
    if not row["depends_on"]:
        return
    waiting = [
        r["task_id"]
        for r in tx.execute(_TASKS, {"e": row["entity_id"], "p": row["period"]}).mappings()
        if r["task_id"] in row["depends_on"] and r["status"] != "done"
    ]
    if waiting:
        raise _conflict("The tasks it depends on aren't done", ", ".join(waiting))


def prepare(
    tx: Connection, entity_id: str, period: str, task_id: str, *, p: Principal, note: str | None, request_id: str | None
) -> RowMapping:
    _period(tx, entity_id, period)
    row = task(tx, entity_id, period, task_id, lock=True)
    if row["check_name"] is not None:
        raise _conflict("An automatic task is completed by its check", f"run the {row['check_name']} check")
    _assigned(row, "preparer", p.user_id)
    if row["status"] != "open":
        raise _conflict(f"Cannot prepare a {row['status']} task")
    _dependencies_done(tx, row)
    status = "prepared" if row["reviewer"] else "done"
    values = {"st": status, "by": p.user_id, "note": note, "checked": None, "evidence": None}
    tx.execute(_PREPARED, {**values, "e": entity_id, "p": period, "t": task_id})
    new = _reload(tx, row)
    _event(tx, new, "prepare", p.user_id, note=note)
    _audit(tx, p.user_id, "prepare", new, request_id)
    return new


def checkable(tx: Connection, entity_id: str, period: str, task_id: str) -> RowMapping:
    """The checks a task's check makes before its read, in the order the transaction makes them again."""
    _period(tx, entity_id, period)
    row = task(tx, entity_id, period, task_id)
    if row["check_name"] is None:
        raise _conflict("A manual task is prepared by its preparer, not checked")
    _dependencies_done(tx, row)
    return row


def record_check(
    tx: Connection,
    entity_id: str,
    period: str,
    task_id: str,
    *,
    p: Principal,
    passed: bool,
    evidence: dict[str, Any],
    checked_at: datetime,
    request_id: str | None,
) -> RowMapping:
    """Record a check that read its data from ``checked_at``. Passing completes the task (or readies it for its
    reviewer, who must review it again); failing leaves it open."""
    _period(tx, entity_id, period)
    row = task(tx, entity_id, period, task_id, lock=True)
    if row["check_name"] is None:
        raise _conflict("A manual task is prepared by its preparer, not checked")
    _dependencies_done(tx, row)
    recorded = {**evidence, "passed": passed}
    if passed:
        status = "prepared" if row["reviewer"] else "done"
        values = {"st": status, "by": p.user_id, "note": None, "checked": checked_at, "evidence": json.dumps(recorded)}
        tx.execute(_PREPARED, {**values, "e": entity_id, "p": period, "t": task_id})
    else:
        tx.execute(
            _REOPENED, {"evidence": json.dumps(recorded, default=str), "e": entity_id, "p": period, "t": task_id}
        )
    new = _reload(tx, row)
    _event(tx, new, "check", p.user_id, detail=recorded)
    _audit(tx, p.user_id, "check", new, request_id, passed=passed)
    return new


def review(
    tx: Connection, entity_id: str, period: str, task_id: str, *, p: Principal, request_id: str | None
) -> RowMapping:
    _period(tx, entity_id, period)
    row = task(tx, entity_id, period, task_id, lock=True)
    _assigned(row, "reviewer", p.user_id)
    if row["status"] != "prepared":
        raise _conflict(f"Cannot review a {row['status']} task", "review needs a prepared task")
    if row["prepared_by"] == p.user_id:
        raise _conflict("A reviewer can't review their own preparation", f"{p.user_id} prepared {task_id}")
    tx.execute(_REVIEWED, {"by": p.user_id, "e": entity_id, "p": period, "t": task_id})
    new = _reload(tx, row)
    _event(tx, new, "review", p.user_id)
    _audit(tx, p.user_id, "review", new, request_id)
    return new


def send_back(
    tx: Connection, entity_id: str, period: str, task_id: str, *, p: Principal, reason: str, request_id: str | None
) -> RowMapping:
    _period(tx, entity_id, period)
    row = task(tx, entity_id, period, task_id, lock=True)
    _assigned(row, "reviewer", p.user_id)
    if row["status"] != "prepared":
        raise _conflict(f"Cannot send back a {row['status']} task", "sending back needs a prepared task")
    tx.execute(_REOPENED, {"evidence": None, "e": entity_id, "p": period, "t": task_id})
    new = _reload(tx, row)
    _event(tx, new, "reject", p.user_id, note=reason)
    _audit(tx, p.user_id, "reject", new, request_id, reason=reason)
    return new


def reopen(
    tx: Connection, entity_id: str, period: str, task_id: str, *, p: Principal, reason: str, request_id: str | None
) -> RowMapping:
    _period(tx, entity_id, period)
    row = task(tx, entity_id, period, task_id, lock=True)
    if row["status"] == "open":
        raise _conflict("The task is already open")
    tx.execute(_REOPENED, {"evidence": None, "e": entity_id, "p": period, "t": task_id})
    new = _reload(tx, row)
    _event(tx, new, "reopen", p.user_id, note=reason)
    _audit(tx, p.user_id, "reopen", new, request_id, reason=reason)
    return new


def reassign(
    tx: Connection,
    entity_id: str,
    period: str,
    task_id: str,
    *,
    p: Principal,
    preparer: str | None,
    reviewer: str | None,
    request_id: str | None,
) -> RowMapping:
    """Give a task that isn't done a new preparer, a new reviewer, or both."""
    _period(tx, entity_id, period)
    row = task(tx, entity_id, period, task_id, lock=True)
    if row["status"] == "done":
        raise _conflict("A done task can't be reassigned", "reopen it first")
    if preparer is not None and row["check_name"] is not None:
        raise _conflict("An automatic task has no preparer")
    new_preparer = preparer if preparer is not None else row["preparer"]
    new_reviewer = reviewer if reviewer is not None else row["reviewer"]
    if new_reviewer is not None and new_reviewer == new_preparer:
        raise _conflict("A task's reviewer can't be its preparer")
    for user, action in ((preparer, Action.CLOSE_PREPARE), (reviewer, Action.CLOSE_REVIEW)):
        if user is not None and not close_configs.holds(tx, user, action, entity_id):
            raise Problem(
                422, "The user can't take the task", f"{user} lacks {action} in {entity_id}", kind="validation-failed"
            )
    tx.execute(_REASSIGNED, {"prep": new_preparer, "rev": new_reviewer, "e": entity_id, "p": period, "t": task_id})
    new = _reload(tx, row)
    detail = {"preparer": [row["preparer"], new_preparer], "reviewer": [row["reviewer"], new_reviewer]}
    _event(tx, new, "reassign", p.user_id, detail=detail)
    _audit(tx, p.user_id, "reassign", new, request_id, **detail)
    return new


def history(tx: Connection, entity_id: str, period: str, task_id: str) -> list[dict[str, Any]]:
    rows = tx.execute(
        text(
            "SELECT action, actor, at, note, detail FROM close_task_events WHERE entity_id = :e AND period = :p "
            "AND task_id = :t ORDER BY at, event_id"
        ),
        {"e": entity_id, "p": period, "t": task_id},
    ).mappings()
    return [{k: _plain(r[k]) for k in ("action", "actor", "at", "note", "detail")} for r in rows]


# ---------------------------------------------------------------------------------------------- reminders


def run_reminders(engine: Engine, as_of: datetime | None = None) -> int:
    """Remind whoever each due task waits on, once a day, and tell the controller once a day when it is overdue by
    the checklist's days. Postgres only, and idempotent: a notification's key holds its day. Returns how many were
    created."""
    created = 0
    with engine.begin() as tx:
        today = (as_of or workflow.now(tx)).date()
        for t in tx.execute(_DUE, {"today": today}).mappings().all():
            ref = f"{t['entity_id']}:{t['period']}:{t['task_id']}"
            waiting_on = t["preparer"] if t["status"] == "open" else t["reviewer"]
            if waiting_on:
                verb = "prepare" if t["status"] == "open" else "review"
                created += _notice(
                    tx,
                    user_id=waiting_on,
                    kind="task_due",
                    key=f"task_due:{ref}:{today}",
                    t=t,
                    message=f"{t['name']}: due {t['due_on']}, {verb} it",
                )
            if (today - t["due_on"]).days >= t["escalate_after_days"]:
                for controller in sorted(holders(tx, t["entity_id"], Action.PERIOD_MANAGE)):
                    message = f"{t['name']} is {(today - t['due_on']).days} days overdue ({t['status']})"
                    key = f"task_escalation:{ref}:{controller}:{today}"
                    created += _notice(tx, user_id=controller, kind="task_escalation", key=key, t=t, message=message)
    return created


def _notice(tx: Connection, *, user_id: str, kind: str, key: str, t: RowMapping, message: str) -> int:
    inserted = notify(tx, user_id=user_id, kind=kind, key=key, entity_id=t["entity_id"], message=message)
    if inserted:
        tx.execute(
            text("UPDATE notifications SET task_ref = :r WHERE dedup_key = :k"),
            {"r": f"{t['entity_id']}:{t['period']}:{t['task_id']}", "k": key},
        )
    return int(inserted)
