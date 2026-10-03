"""The journal workflow's scheduled tasks. Each takes an as-of time (Postgres ``clock_timestamp()`` by default), so
tests move time without sleeping, and each is idempotent, as every scheduler task must be (ADR-0007).

- **Recurring journals** (:func:`run_recurring`). Each active recurring journal gets one journal per due period, on
  its day. The unique index on ``(recurring_id, recurring_period)`` backs this. The journal is created as a draft
  for its preparer, or approved directly when it is marked ``auto_approve`` and the approval matrix lets it bypass
  approval (flagged). A period that isn't open, or a preparer who has lost the role, skips the journal, and the
  preparer is notified once.
- **Reminders and escalation** (:func:`run_reminders`). A journal or batch waiting at a level gets a reminder to
  that level's eligible approvers after ``first_after_hours``, then every ``repeat_every_hours``. After
  ``escalate_after`` reminders, each further reminder also notifies the next level's approvers and the controller.
  Notification dedup keys make each one once.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Final

from sqlalchemy import Connection, Engine, text

from close_app import workflow_configs
from close_app.authz import APPROVE_LEVEL, POLICY, Action, Role
from close_app.errors import Problem
from close_app.journals import store, workflow
from close_app.journals.setup import Setup, active
from close_contracts.config.journal_workflow import RecurringJournal
from close_contracts.fx import Rate, RateKey
from close_contracts.journals import from_recurring, may_bypass
from close_platform.query.rates import RateLookup

_NOTIFY: Final = text(
    "INSERT INTO notifications (notification_id, user_id, kind, dedup_key, entity_id, journal_id, batch_id, message) "
    "VALUES (:id, :u, :k, :key, :e, :j, :b, :msg) ON CONFLICT (dedup_key) DO NOTHING RETURNING notification_id"
)
_HOLDERS: Final = text(
    "SELECT DISTINCT user_id FROM role_assignments WHERE role = ANY (:roles) AND scope = :e AND revoked_at IS NULL"
)
_EXISTS: Final = text("SELECT 1 FROM journals WHERE recurring_id = :r AND recurring_period = :p")
_PERIOD: Final = text("SELECT status FROM accounting_periods WHERE entity_id = :e AND period = :p")
_WAITING_JOURNALS: Final = text(
    "SELECT journal_id AS id, NULL::uuid AS batch_id, entity_id, number::text AS label, created_by, submission, "
    "current_level, level_since FROM journals WHERE status = 'pending_approval' AND batch_id IS NULL"
)
_WAITING_BATCHES: Final = text(
    "SELECT NULL::uuid AS id, batch_id, entity_id, 'batch ' || batch_id::text AS label, created_by, submission, "
    "current_level, level_since FROM journal_batches WHERE status = 'pending_approval'"
)
_BATCH_PREPARERS: Final = text("SELECT DISTINCT created_by FROM journals WHERE batch_id = :b")
_APPROVED_BY: Final = text(
    "SELECT approver, on_behalf_of FROM journal_approvals WHERE journal_id = :j AND submission = :s"
)
_BATCH_APPROVED_BY: Final = text(
    "SELECT DISTINCT a.approver, a.on_behalf_of FROM journal_approvals a "
    "JOIN journals j ON j.journal_id = a.journal_id WHERE a.batch_id = :b AND a.submission = j.submission"
)


def notify(
    tx: Connection,
    *,
    user_id: str,
    kind: str,
    key: str,
    entity_id: str,
    message: str,
    journal_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> bool:
    inserted = tx.execute(
        _NOTIFY,
        {
            "id": uuid.uuid4(),
            "u": user_id,
            "k": kind,
            "key": key,
            "e": entity_id,
            "j": journal_id,
            "b": batch_id,
            "msg": message,
        },
    ).first()
    return inserted is not None


def holders(tx: Connection, entity_id: str, action: Action) -> set[str]:
    roles = [r.value for r in Role if action in POLICY[r]]
    return {str(u) for (u,) in tx.execute(_HOLDERS, {"roles": roles, "e": entity_id})}


def _months(first: str, last: str) -> list[str]:
    out, year, month = [], int(first[:4]), int(first[5:])
    while f"{year}-{month:02d}" <= last:
        out.append(f"{year}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _due(recurring: RecurringJournal, at: datetime) -> list[str]:
    last = store.period_of(at.date())
    if recurring.last_period is not None:
        last = min(last, recurring.last_period)
    return [
        p
        for p in _months(recurring.first_period, last)
        if recurring.due(p) and at.date() >= date(int(p[:4]), int(p[5:]), recurring.day)
    ]


def _rate(
    engine: Engine, recurring: RecurringJournal, day: date, rates: RateLookup | None
) -> tuple[Rate | None, str | None]:
    """The rate an auto-approved foreign-currency journal needs to bypass approval, looked up before the
    transaction; or why it can't be had (the journal is then skipped this time and tried again next run)."""
    if not recurring.auto_approve or recurring.currency is None:
        return None, None
    with engine.connect() as conn:
        functional = active(conn).entity(recurring.entity_id).functional_currency
    if recurring.currency == functional:
        return None, None
    key = RateKey(recurring.currency, functional, day)
    if rates is None:
        return None, "exchange rates are unavailable"
    found = rates.rates([key]).get(key)
    return (found, None) if found is not None else (None, f"no {key.label()} rate has been published yet")


def _one_recurring(engine: Engine, recurring: RecurringJournal, period: str, rates: RateLookup | None) -> bool:
    day = date(int(period[:4]), int(period[5:]), recurring.day)
    rate, missing = _rate(engine, recurring, day, rates)
    with engine.begin() as tx:
        if tx.execute(_EXISTS, {"r": recurring.recurring_id, "p": period}).first() is not None:
            return False
        setup = active(tx)
        status = tx.execute(_PERIOD, {"e": recurring.entity_id, "p": period}).scalar()
        reason = None
        if status not in store.TAKES_AUTOMATED:
            reason = f"period {period} is {status or 'not opened'}"
        elif recurring.preparer not in holders(tx, recurring.entity_id, Action.JOURNAL_PREPARE):
            reason = f"{recurring.preparer} is no longer a preparer in {recurring.entity_id}"
        elif missing is not None:
            reason = missing
        if reason is None:
            try:
                return _create_recurring(tx, setup, recurring, period, rate)
            except Problem as exc:
                reason = f"{exc.title}: {exc.detail or ''}".rstrip(": ")
        notify(
            tx,
            user_id=recurring.preparer,
            kind="recurring_skipped",
            key=f"recurring_skipped:{recurring.recurring_id}:{period}",
            entity_id=recurring.entity_id,
            message=f"recurring journal {recurring.recurring_id} wasn't created for {period}: {reason}",
        )
        return False


def _create_recurring(
    tx: Connection, setup: Setup, recurring: RecurringJournal, period: str, rate: Rate | None
) -> bool:
    day = date(int(period[:4]), int(period[5:]), recurring.day)
    savepoint = tx.begin_nested()
    try:
        row = workflow.create(
            tx,
            setup,
            from_recurring(recurring, day),
            created_by=recurring.preparer,
            actor=store.SYSTEM,
            request_id=None,
            source="recurring",
            recurring=(recurring.recurring_id, period),
        )
        frozen = workflow.frozen_amounts(tx, setup, row, rate) if recurring.auto_approve else None
        if frozen is not None and may_bypass("recurring", frozen.debits, row["functional_currency"], setup.matrix):
            reason = "recurring journal; the approval matrix lets it skip approval"
            row = workflow.bypass(tx, setup, row, reason, None, frozen=frozen)
            workflow.auto_reverse(tx, setup, row, None)
        savepoint.commit()
    except Problem:
        savepoint.rollback()
        raise
    return True


def run_recurring(engine: Engine, *, as_of: datetime | None = None, rates: RateLookup | None = None) -> int:
    """Create every recurring journal due by ``as_of``. Returns how many were created. ``rates`` serves the
    auto-approved ones in a foreign currency, which are converted as they bypass approval."""
    with engine.begin() as tx:
        at = as_of or workflow.now(tx)
        recurring = workflow_configs.context(tx, ("active",)).recurring
    created = 0
    for _, r in sorted(recurring.items()):
        for period in _due(r, at):
            created += _one_recurring(engine, r, period, rates)
    return created


def run_reminders(engine: Engine, *, as_of: datetime | None = None) -> int:
    """Write the reminders and escalations due by ``as_of``. Returns how many notifications were written."""
    written = 0
    with engine.begin() as tx:
        try:
            setup = active(tx)
        except Problem:
            return 0  # nothing can be waiting before the workflow is set up
        at = as_of or workflow.now(tx)
        rules = setup.matrix.reminders
        waiting = [*tx.execute(_WAITING_JOURNALS).mappings(), *tx.execute(_WAITING_BATCHES).mappings()]
        for item in waiting:
            hours = (at - item["level_since"]).total_seconds() / 3600
            if hours < rules.first_after_hours:
                continue
            due = 1 + int((hours - rules.first_after_hours) // rules.repeat_every_hours)
            level, entity_id = int(item["current_level"]), str(item["entity_id"])
            if item["batch_id"] is not None:
                involved = {item["created_by"]} | {
                    str(u) for (u,) in tx.execute(_BATCH_PREPARERS, {"b": item["batch_id"]})
                }
                approvals = tx.execute(_BATCH_APPROVED_BY, {"b": item["batch_id"]}).all()
            else:
                involved = {str(item["created_by"])}
                approvals = tx.execute(_APPROVED_BY, {"j": item["id"], "s": item["submission"]}).all()
            involved |= {str(a) for row in approvals for a in row if a}
            subject = f"{'batch' if item['batch_id'] else 'journal'}:{item['batch_id'] or item['id']}"
            base = f"{subject}:{item['submission']}:{level}"
            for seq in range(1, due + 1):
                for user in sorted(holders(tx, entity_id, APPROVE_LEVEL[level]) - involved):
                    written += notify(
                        tx,
                        user_id=user,
                        kind="reminder",
                        key=f"reminder:{base}:{seq}:{user}",
                        entity_id=entity_id,
                        journal_id=item["id"],
                        batch_id=item["batch_id"],
                        message=f"{entity_id} {item['label']} has waited {int(hours)} h for level {level} approval",
                    )
            escalations = due - rules.escalate_after
            if escalations > 0:
                above = APPROVE_LEVEL.get(level + 1)
                targets = (holders(tx, entity_id, above) if above else set()) | holders(
                    tx, entity_id, Action.PERIOD_MANAGE
                )
                for seq in range(1, escalations + 1):
                    for user in sorted(targets - involved):
                        written += notify(
                            tx,
                            user_id=user,
                            kind="escalation",
                            key=f"escalation:{base}:{seq}:{user}",
                            entity_id=entity_id,
                            journal_id=item["id"],
                            batch_id=item["batch_id"],
                            message=f"{entity_id} {item['label']} is overdue at level {level} ({int(hours)} h)",
                        )
    return written
