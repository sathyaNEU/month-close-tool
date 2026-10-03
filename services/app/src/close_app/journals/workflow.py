"""The journal state machine (Stage 7a, ADR-0016).

    draft --submit--> pending_approval (level 1 .. n) --approve at level n--> approved --reverse--> a new draft
      ^                    |  reject (an approver), recall (the preparer)
      +--------------------+
    draft --cancel--> cancelled

- **Routing is frozen at submit.** The journal keeps the matrix version, the risk score with its factors and
  inputs, and the levels it needs. A recall or rejection returns it to draft, and the next submission starts over,
  so earlier approvals stay as history but no longer count.
- **Approval authority** is the role for the level (an L2 approver may approve level 1 or 2), held directly or
  through an active delegation from someone who holds it.
- **Separation of duties.** The preparer can't approve or reject their own journal. One person approves at most
  one level of a submission, counting both the approver and the person they stand in for. Each refusal is
  audited.
- **Periods.** Every move locks the journal row, and then its period row ``FOR SHARE``. Closing a period locks
  that row ``FOR UPDATE``, so a period can't close while a journal in it is being submitted or approved.
- **Automatic reversals.** When an approved journal's template reverses automatically, its reversal is created on
  the first day of the next period: at approval if that period is open, otherwise when it opens.
- **Foreign currencies (Stage 7c, ADR-0018).** A journal in a currency other than its entity's functional one is
  converted when it is submitted (or bypasses approval), at a rate the caller looked up before the transaction.
  The conversion is frozen per submission and bound into every approval, routing and bypass caps read the
  functional total, and a reversal that is still the plain flip of its original reuses the original's rate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from close_app.authz import APPROVE_LEVEL, Action, holds
from close_app.errors import Denied, Problem
from close_app.guard import deny
from close_app.identity import Principal
from close_app.journals import store
from close_app.journals.setup import Setup, checked
from close_contracts.canonical_json import content_sha256
from close_contracts.fx import Rate, RateKey
from close_contracts.journals import (
    Conversion,
    JournalDraft,
    RiskContext,
    RoutingError,
    content_hash,
    convert,
    in_functional,
    may_bypass,
    reversal_of,
    reverses_exactly,
    risk_score,
    route,
)

_PERIOD_SHARE: Final = text("SELECT * FROM accounting_periods WHERE entity_id = :e AND period = :p FOR SHARE")
_USED_ACCOUNTS: Final = text(
    "SELECT DISTINCT l.account FROM journal_lines l JOIN journals j "
    "ON j.journal_id = l.journal_id AND l.revision = j.revision "
    "WHERE j.entity_id = :e AND j.status = ANY (:done) AND j.approved_at >= :since AND l.account = ANY (:accounts)"
)
_PREPARER_JOURNALS: Final = text(
    "SELECT count(*) FROM journals WHERE entity_id = :e AND created_by = :u AND created_at >= :since "
    "AND journal_id <> :id AND status <> 'cancelled'"
)
_ATTACHMENTS: Final = text("SELECT count(*) FROM attachments WHERE journal_id = :id AND removed_at IS NULL")
_EDIT: Final = text(
    "UPDATE journals SET template_id = :t, template_version = :tv, period = :p, accounting_date = :d, "
    "description = :desc, header = CAST(:h AS jsonb), auto_reverse = :ar, revision = :r, content_hash = :hash, "
    "total_debit_minor = :dr, total_credit_minor = :cr, line_count = :lc, currency = :cur, "
    "row_version = row_version + 1 WHERE journal_id = :id"
)
_SUBMIT: Final = text(
    "UPDATE journals SET status = 'pending_approval', submission = submission + 1, submitted_by = :by, "
    "submitted_at = :at, matrix_id = :mid, matrix_version = :mv, routing = CAST(:routing AS jsonb), "
    "risk_score = :rs, risk_version = :rv, risk_factors = CAST(:rf AS jsonb), required_levels = :lv, "
    "current_level = 1, level_since = :at, functional_debit_minor = :fd, functional_credit_minor = :fcr, "
    "ledger_line_count = :llc, conversion_hash = :ch, row_version = row_version + 1 WHERE journal_id = :id"
)
_BACK_TO_DRAFT: Final = text(
    "UPDATE journals SET status = 'draft', current_level = NULL, level_since = NULL, functional_debit_minor = NULL, "
    "functional_credit_minor = NULL, ledger_line_count = NULL, conversion_hash = NULL, row_version = row_version + 1 "
    "WHERE journal_id = :id"
)
_CANCEL: Final = text(
    "UPDATE journals SET status = 'cancelled', cancelled_by = :by, cancelled_at = clock_timestamp(), "
    "row_version = row_version + 1 WHERE journal_id = :id"
)
_RECORD_APPROVAL: Final = text(
    "INSERT INTO journal_approvals (journal_id, submission, level, approver, on_behalf_of, content_hash, "
    "conversion_hash, batch_id, approved_at) VALUES (:id, :s, :l, :by, :obo, :hash, :ch, :batch, :at)"
)
_NEXT_LEVEL: Final = text(
    "UPDATE journals SET current_level = current_level + 1, level_since = :at, row_version = row_version + 1 "
    "WHERE journal_id = :id"
)
_APPROVED: Final = text(
    "UPDATE journals SET status = 'approved', approved_at = :at, current_level = NULL, level_since = NULL, "
    "row_version = row_version + 1 WHERE journal_id = :id"
)
_BYPASS: Final = text(
    "UPDATE journals SET status = 'approved', approved_at = clock_timestamp(), bypass_reason = :why, "
    "matrix_id = :mid, matrix_version = :mv, functional_debit_minor = :fd, functional_credit_minor = :fcr, "
    "ledger_line_count = :llc, conversion_hash = :ch, row_version = row_version + 1 WHERE journal_id = :id"
)
_LIVE_REVERSAL: Final = text("SELECT 1 FROM journals WHERE reverses_journal_id = :id AND status <> 'cancelled'")
_DELEGATORS: Final = text(
    "SELECT delegator FROM delegations WHERE delegate = :u AND :e = ANY (entity_ids) AND starts_on <= :d "
    "AND ends_on >= :d AND revoked_at IS NULL ORDER BY created_at, delegation_id"
)
_ASSIGNMENTS: Final = text("SELECT role, scope FROM role_assignments WHERE user_id = :u AND revoked_at IS NULL")


def conflict(title: str, detail: str) -> Problem:
    return Problem(409, title, detail, kind="conflict")


def now(tx: Connection) -> datetime:
    at: datetime = tx.execute(text("SELECT clock_timestamp()")).scalar_one()
    return at


def next_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:])
    return f"{year + month // 12}-{month % 12 + 1:02d}"


def open_period(tx: Connection, entity_id: str, period: str) -> RowMapping:
    """The period row, locked ``FOR SHARE`` until the transaction ends, or a 409 unless it takes people's entries
    (open, or soft-closed: Stage 8b)."""
    row = tx.execute(_PERIOD_SHARE, {"e": entity_id, "p": period}).mappings().first()
    if row is None:
        raise conflict("The period isn't open", f"{entity_id} {period} has not been opened")
    if row["status"] not in store.TAKES_MANUAL:
        raise conflict("The period isn't open", f"{entity_id} {period} is {row['status']}")
    return row


def need_status(row: RowMapping, *statuses: str, verb: str) -> None:
    if row["status"] not in statuses:
        raise conflict(f"Cannot {verb} a {row['status']} journal", f"{verb} needs a {' or '.join(statuses)} journal")


def not_in_batch(row: RowMapping) -> None:
    if row["batch_id"] is not None:
        raise conflict("The journal is part of a batch", f"act on batch {row['batch_id']} instead")


def preparer_only(p: Principal, row: RowMapping, verb: str) -> None:
    if p.user_id != row["created_by"]:
        raise Denied(
            p.user_id,
            Action.JOURNAL_PREPARE.value,
            target=store.target(row),
            entity_ids=(row["entity_id"],),
            sod=f"only the journal's preparer can {verb}",
        )


# ---------------------------------------------------------------------------------------------- drafts


def create(
    tx: Connection,
    setup: Setup,
    draft: JournalDraft,
    *,
    created_by: str,
    actor: str,
    request_id: str | None,
    source: str = "manual",
    batch_id: UUID | None = None,
    import_id: UUID | None = None,
    recurring: tuple[str, str] | None = None,
    reverses: UUID | None = None,
    auto_reverse: bool | None = None,
) -> RowMapping:
    draft = in_functional(draft, setup.entity(draft.entity_id).functional_currency)
    template, version, entity = checked(setup, draft)
    open_period(tx, draft.entity_id, store.period_of(draft.accounting_date))
    row = store.insert(
        tx,
        draft,
        template_version=version,
        functional_currency=entity.functional_currency,
        auto_reverse=template.auto_reverse if auto_reverse is None else auto_reverse,
        source=source,
        created_by=created_by,
        batch_id=batch_id,
        import_id=import_id,
        recurring=recurring,
        reverses=reverses,
    )
    store.history(tx, action="create", actor=actor, journal=row, to_status="draft")
    store.audit_event(tx, actor, "create", row, request_id, source=source, preparer=created_by)
    return row


def edit(
    tx: Connection, setup: Setup, row: RowMapping, draft: JournalDraft, *, p: Principal, request_id: str | None
) -> RowMapping:
    need_status(row, "draft", verb="edit")
    preparer_only(p, row, "edit it")
    if draft.entity_id != row["entity_id"]:
        raise Problem(422, "A journal can't move to another entity", kind="validation-failed")
    draft = in_functional(draft, row["functional_currency"])
    template, version, _ = checked(setup, draft)
    open_period(tx, draft.entity_id, store.period_of(draft.accounting_date))
    revision = row["revision"] + 1
    store.write_lines(tx, row["journal_id"], revision, draft)
    tx.execute(
        _EDIT,
        {
            "id": row["journal_id"],
            "t": draft.template_id,
            "tv": version,
            "p": store.period_of(draft.accounting_date),
            "d": draft.accounting_date,
            "desc": draft.description,
            "h": json.dumps(draft.header, sort_keys=True),
            "ar": template.auto_reverse and row["source"] != "reversal",
            "r": revision,
            "hash": content_hash(draft),
            "dr": draft.total_debits(),
            "cr": draft.total_credits(),
            "lc": len(draft.lines),
            "cur": draft.currency or row["functional_currency"],
        },
    )
    new = store.reload(tx, row)
    store.history(tx, action="edit", actor=p.user_id, journal=new, from_status="draft", to_status="draft")
    store.audit_event(tx, p.user_id, "edit", new, request_id)
    return new


def cancel(tx: Connection, row: RowMapping, *, p: Principal, request_id: str | None) -> RowMapping:
    need_status(row, "draft", verb="cancel")
    if row["batch_id"] is not None:
        raise conflict("The journal is part of a batch", "take it out of the batch first")
    is_controller = holds(p.assignments, Action.PERIOD_MANAGE, row["entity_id"])
    if not is_controller:
        preparer_only(p, row, "cancel it")
    tx.execute(_CANCEL, {"id": row["journal_id"], "by": p.user_id})
    new = store.reload(tx, row)
    store.history(tx, action="cancel", actor=p.user_id, journal=new, from_status="draft", to_status="cancelled")
    store.audit_event(tx, p.user_id, "cancel", new, request_id)
    return new


# ---------------------------------------------------------------------------------------------- submit


def _risk_context(tx: Connection, setup: Setup, row: RowMapping, draft: JournalDraft, at: datetime) -> RiskContext:
    scoring = setup.matrix.risk
    period = open_period(tx, row["entity_id"], row["period"])
    accounts = sorted({ln.account for ln in draft.lines})
    used = {
        str(a)
        for (a,) in tx.execute(
            _USED_ACCOUNTS,
            {
                "e": row["entity_id"],
                "done": list(store.APPROVED_OR_LATER),  # a posted journal still used its accounts
                "since": at - timedelta(days=scoring.seldom_used_days),
                "accounts": accounts,
            },
        )
    }
    preparer_journals = tx.execute(
        _PREPARER_JOURNALS,
        {
            "e": row["entity_id"],
            "u": row["created_by"],
            "since": at - timedelta(days=scoring.infrequent_preparer_days),
            "id": row["journal_id"],
        },
    ).scalar_one()
    return RiskContext(
        submitted_on=at.date(),
        period_end=period["ends_on"],
        preparer_journals=int(preparer_journals),
        unused_accounts=tuple(a for a in accounts if a not in used),
        suspense_account=setup.chart.suspense_account,
    )


# ---------------------------------------------------------------------------------------------- foreign currencies


@dataclass(frozen=True)
class RateNeed:
    """What a journal needs to be converted: nothing (functional currency), the original's rate (an exact
    reversal), or a rate to look up before the transaction (``key``)."""

    key: RateKey | None = None
    reused: Rate | None = None
    reused_from: str | None = None


def rate_need(tx: Connection, row: RowMapping) -> RateNeed:
    if row["currency"] == row["functional_currency"]:
        return RateNeed()
    original = store.load(tx, row["reverses_journal_id"]) if row["reverses_journal_id"] else None
    if original is not None and original["conversion_hash"]:
        frozen = store.conversion(tx, original)
        if frozen is not None and reverses_exactly(store.draft_of(tx, row), store.draft_of(tx, original)):
            return RateNeed(reused=Rate.from_json(frozen["detail"]["rate"]), reused_from=str(original["journal_id"]))
    return RateNeed(key=RateKey(row["currency"], row["functional_currency"], row["accounting_date"]))


def no_rate(key: RateKey) -> Problem:
    issue = {"code": "no_rate", "path": "/currency", "message": f"no {key.label()} rate has been published yet"}
    return Problem(422, "No exchange rate", f"there is no {key.label()} rate", kind="validation-failed", issues=[issue])


@dataclass(frozen=True)
class Frozen:
    """A journal's functional totals as a submission (or a bypass) freezes them, with its conversion if any."""

    debits: int
    credits: int
    ledger_lines: int
    conversion: Conversion | None

    def params(self) -> dict[str, object]:
        hashed = self.conversion.hash() if self.conversion is not None else None
        return {"fd": self.debits, "fcr": self.credits, "llc": self.ledger_lines, "ch": hashed}


def frozen_amounts(tx: Connection, setup: Setup, row: RowMapping, rate: Rate | None) -> Frozen:
    """The functional totals, converting a foreign-currency journal at ``rate`` (looked up by the caller, outside
    the transaction) or at its original's rate. A missing rate, or one for another date or currency than the
    journal now has (it changed while the rate was read), is refused."""
    need = rate_need(tx, row)
    if need.key is None and need.reused is None:
        return Frozen(row["total_debit_minor"], row["total_credit_minor"], row["line_count"], None)
    used = need.reused or rate
    if used is None:
        raise no_rate(need.key) if need.key is not None else RuntimeError("no rate and no key")
    if need.key is not None and used.key != need.key:
        raise conflict("The journal changed", f"its rate was read as {used.key.label()}; submit it again")
    conversion = convert(
        store.draft_of(tx, row),
        functional=row["functional_currency"],
        rate=used,
        rounding_account=setup.chart.fx_rounding_account,
        reused_from=need.reused_from,
    )
    rounding = 1 if conversion.rounding_side else 0
    total = conversion.functional_total
    return Frozen(total, total, row["line_count"] + rounding, conversion)


def routed(tx: Connection, setup: Setup, row: RowMapping, at: datetime, frozen: Frozen) -> dict[str, object]:
    """Check the journal against the active configuration and work out its routing, frozen at submit."""
    draft = store.draft_of(tx, row)
    template, _, _ = checked(setup, draft)
    required_from = template.attachment_required_from_minor
    needs_document = required_from is not None and frozen.debits >= required_from
    if needs_document and tx.execute(_ATTACHMENTS, {"id": row["journal_id"]}).scalar_one() == 0:
        detail = f"template {template.template_id} needs supporting documents from {required_from} (minor units)"
        raise conflict("An attachment is required", detail)
    risk = risk_score(draft, setup.matrix.risk, _risk_context(tx, setup, row, draft, at))
    try:
        routing = route(
            draft, currency=row["functional_currency"], total_minor=frozen.debits, matrix=setup.matrix, risk=risk.score
        )
    except RoutingError as exc:
        raise conflict("The approval matrix can't route this journal", str(exc)) from None
    return {
        "mid": setup.matrix.matrix_id,
        "mv": setup.matrix_version,
        "routing": json.dumps(
            {
                "levels": routing.levels,
                "amount_levels": routing.amount_levels,
                "risk_levels": routing.risk_levels,
                "rule_levels": routing.rule_levels,
                "rules": list(routing.rules),
                "total_minor": routing.total_minor,
            },
            sort_keys=True,
        ),
        "rs": risk.score,
        "rv": risk.version,
        "rf": json.dumps(risk.factors, sort_keys=True),
        "lv": routing.levels,
        **frozen.params(),
    }


def submit(
    tx: Connection,
    setup: Setup,
    row: RowMapping,
    *,
    p: Principal,
    request_id: str | None,
    as_of: datetime | None = None,
    rate: Rate | None = None,
) -> RowMapping:
    need_status(row, "draft", verb="submit")
    not_in_batch(row)
    preparer_only(p, row, "submit it")
    at = as_of or now(tx)
    frozen = frozen_amounts(tx, setup, row, rate)
    params = routed(tx, setup, row, at, frozen)
    return mark_submitted(tx, row, params, actor=p.user_id, at=at, request_id=request_id, conversion=frozen.conversion)


def mark_submitted(
    tx: Connection,
    row: RowMapping,
    frozen: dict[str, object],
    *,
    actor: str,
    at: datetime,
    request_id: str | None,
    levels: int | None = None,
    conversion: Conversion | None = None,
) -> RowMapping:
    """Freeze the routing and wait for level 1. A batch passes its own ``levels``, the most any member needs."""
    params = {**frozen, "lv": levels} if levels is not None else frozen
    tx.execute(_SUBMIT, {"id": row["journal_id"], "by": actor, "at": at, **params})
    new = store.reload(tx, row)
    if conversion is not None:
        store.insert_conversion(tx, new, conversion, submission=new["submission"])
    store.history(
        tx,
        action="submit",
        actor=actor,
        journal=new,
        batch_id=row["batch_id"],
        from_status="draft",
        to_status="pending_approval",
        submission=new["submission"],
    )
    store.audit_event(
        tx, actor, "submit", new, request_id, required_levels=new["required_levels"], risk_score=new["risk_score"]
    )
    return new


# ---------------------------------------------------------------------------------------------- approve


@dataclass(frozen=True)
class Acting:
    approver: str
    on_behalf_of: str | None

    def people(self) -> set[str]:
        return {self.approver} | ({self.on_behalf_of} if self.on_behalf_of else set())


def acting(tx: Connection, p: Principal, entity_id: str, level: int, on: date) -> Acting | None:
    """Who is approving: the caller with the level's authority, or on behalf of a delegator who has it."""
    action = APPROVE_LEVEL[level]
    if holds(p.assignments, action, entity_id):
        return Acting(p.user_id, None)
    for (delegator,) in tx.execute(_DELEGATORS, {"u": p.user_id, "e": entity_id, "d": on}):
        theirs = frozenset((str(r.role), str(r.scope)) for r in tx.execute(_ASSIGNMENTS, {"u": delegator}))
        if holds(theirs, action, entity_id):
            return Acting(p.user_id, str(delegator))
    return None


def may_approve(tx: Connection, p: Principal, entity_id: str) -> bool:
    """Whether the caller has any approval authority in the entity, held directly or through a delegation.
    Checked before a journal's state, so authorization never depends on it."""
    on = now(tx).date()
    return any(acting(tx, p, entity_id, level, on) is not None for level in APPROVE_LEVEL)


def approver_for(
    tx: Connection,
    p: Principal,
    *,
    entity_id: str,
    level: int,
    preparers: set[str],
    target: str,
    verb: str,
    at: datetime,
) -> Acting:
    """The caller's authority for ``level`` (a 403 without it), and that nobody involved prepared the journal."""
    who = acting(tx, p, entity_id, level, at.date())
    if who is None:
        raise deny(p, APPROVE_LEVEL[level], target, [entity_id])
    if who.people() & preparers:
        stand_in = f" (you stand in for {who.on_behalf_of})" if who.on_behalf_of else ""
        raise Denied(
            p.user_id,
            APPROVE_LEVEL[level].value,
            target=target,
            entity_ids=(entity_id,),
            sod=f"the preparer can't {verb} their own journal{stand_in}",
        )
    return who


def one_level_each(tx: Connection, p: Principal, row: RowMapping, who: Acting, level: int) -> None:
    for earlier in store.approvals(tx, row):
        people = {earlier["approver"]} | ({earlier["on_behalf_of"]} if earlier["on_behalf_of"] else set())
        if people & who.people():
            raise Denied(
                p.user_id,
                APPROVE_LEVEL[level].value,
                target=store.target(row),
                entity_ids=(row["entity_id"],),
                sod=f"{sorted(people & who.people())[0]} already approved level {earlier['level']} of this journal",
            )


def record_approval(
    tx: Connection, row: RowMapping, who: Acting, *, level: int, at: datetime, batch_id: UUID | None = None
) -> None:
    if content_hash(store.draft_of(tx, row)) != row["content_hash"]:  # only drafts change; this is a second guard
        raise conflict("The journal changed", "its lines no longer match what was submitted")
    if row["conversion_hash"]:  # the frozen conversion is insert-only; this is the same second guard for it
        frozen = store.conversion(tx, row)
        if frozen is None or frozen["conversion_hash"] != row["conversion_hash"]:
            raise conflict("The conversion changed", "it no longer matches what was submitted")
        if content_sha256(dict(frozen["detail"])) != row["conversion_hash"]:
            raise conflict("The conversion changed", "its detail no longer matches its hash")
    tx.execute(
        _RECORD_APPROVAL,
        {
            "id": row["journal_id"],
            "s": row["submission"],
            "l": level,
            "by": who.approver,
            "obo": who.on_behalf_of,
            "hash": row["content_hash"],
            "ch": row["conversion_hash"],
            "batch": batch_id,
            "at": at,
        },
    )


def advance(
    tx: Connection,
    setup: Setup,
    row: RowMapping,
    who: Acting,
    *,
    level: int,
    at: datetime,
    request_id: str | None,
) -> RowMapping:
    """Move past an approved level: to the next one, or to approved after the last."""
    final = level >= row["required_levels"]
    tx.execute(_APPROVED if final else _NEXT_LEVEL, {"id": row["journal_id"], "at": at})
    new = store.reload(tx, row)
    store.history(
        tx,
        action="approve",
        actor=who.approver,
        on_behalf_of=who.on_behalf_of,
        journal=new,
        batch_id=row["batch_id"],
        from_status="pending_approval",
        to_status=new["status"],
        submission=new["submission"],
        level=level,
    )
    store.audit_event(tx, who.approver, "approve", new, request_id, level=level, on_behalf_of=who.on_behalf_of)
    if final:
        auto_reverse(tx, setup, new, request_id)
    return new


def approve(
    tx: Connection,
    setup: Setup,
    row: RowMapping,
    *,
    p: Principal,
    request_id: str | None,
    as_of: datetime | None = None,
) -> RowMapping:
    need_status(row, "pending_approval", verb="approve")
    not_in_batch(row)
    open_period(tx, row["entity_id"], row["period"])
    at = as_of or now(tx)
    level = int(row["current_level"])
    target = store.target(row)
    who = approver_for(
        tx,
        p,
        entity_id=row["entity_id"],
        level=level,
        preparers={row["created_by"]},
        target=target,
        verb="approve",
        at=at,
    )
    one_level_each(tx, p, row, who, level)
    record_approval(tx, row, who, level=level, at=at)
    return advance(tx, setup, row, who, level=level, at=at, request_id=request_id)


def reject(
    tx: Connection, row: RowMapping, *, p: Principal, reason: str, request_id: str | None, as_of: datetime | None = None
) -> RowMapping:
    need_status(row, "pending_approval", verb="reject")
    not_in_batch(row)
    at = as_of or now(tx)
    level = int(row["current_level"])
    who = approver_for(
        tx,
        p,
        entity_id=row["entity_id"],
        level=level,
        preparers={row["created_by"]},
        target=store.target(row),
        verb="reject",
        at=at,
    )
    return back_to_draft(
        tx,
        row,
        action="reject",
        actor=who.approver,
        on_behalf_of=who.on_behalf_of,
        reason=reason,
        request_id=request_id,
    )


def recall(tx: Connection, row: RowMapping, *, p: Principal, request_id: str | None) -> RowMapping:
    need_status(row, "pending_approval", verb="recall")
    not_in_batch(row)
    preparer_only(p, row, "recall it")
    return back_to_draft(
        tx, row, action="recall", actor=p.user_id, on_behalf_of=None, reason=None, request_id=request_id
    )


def back_to_draft(
    tx: Connection,
    row: RowMapping,
    *,
    action: str,
    actor: str,
    on_behalf_of: str | None,
    reason: str | None,
    request_id: str | None,
) -> RowMapping:
    tx.execute(_BACK_TO_DRAFT, {"id": row["journal_id"]})
    new = store.reload(tx, row)
    store.history(
        tx,
        action=action,
        actor=actor,
        on_behalf_of=on_behalf_of,
        journal=new,
        batch_id=row["batch_id"],
        from_status="pending_approval",
        to_status="draft",
        submission=new["submission"],
        level=row["current_level"],
        reason=reason,
    )
    store.audit_event(tx, actor, action, new, request_id, reason=reason, on_behalf_of=on_behalf_of)
    return new


# ---------------------------------------------------------------------------------------------- reversals


def _reversal_draft(tx: Connection, row: RowMapping, day: date, prefix: str) -> JournalDraft:
    description = f"{prefix} {row['entity_id']}-{row['number']}: {row['description']}"[:1000]
    return reversal_of(store.draft_of(tx, row), day, description)


def reverse(
    tx: Connection,
    setup: Setup,
    row: RowMapping,
    *,
    p: Principal,
    accounting_date: date | None,
    request_id: str | None,
) -> RowMapping:
    need_status(row, "approved", "posted", "erp_confirmed", verb="reverse")
    if tx.execute(_LIVE_REVERSAL, {"id": row["journal_id"]}).first() is not None:
        raise conflict("The journal already has a reversal", "cancel that reversal first")
    day = accounting_date or open_period(tx, row["entity_id"], next_period(row["period"]))["starts_on"]
    return create(
        tx,
        setup,
        _reversal_draft(tx, row, day, "Reversal of"),
        created_by=p.user_id,
        actor=p.user_id,
        request_id=request_id,
        source="reversal",
        reverses=row["journal_id"],
        auto_reverse=False,
    )


def bypass(
    tx: Connection, setup: Setup, row: RowMapping, reason: str, request_id: str | None, *, frozen: Frozen
) -> RowMapping:
    """Approve without an approver, as the matrix allows. The journal is flagged, and the exception report lists it."""
    tx.execute(
        _BYPASS,
        {
            "id": row["journal_id"],
            "why": reason,
            "mid": setup.matrix.matrix_id,
            "mv": setup.matrix_version,
            **frozen.params(),
        },
    )
    new = store.reload(tx, row)
    if frozen.conversion is not None:
        store.insert_conversion(tx, new, frozen.conversion, submission=new["submission"])
    store.history(
        tx, action="bypass", actor=store.SYSTEM, journal=new, from_status="draft", to_status="approved", reason=reason
    )
    store.audit_event(tx, store.SYSTEM, "bypass", new, request_id, reason=reason)
    return new


def auto_reverse(tx: Connection, setup: Setup, row: RowMapping, request_id: str | None) -> RowMapping | None:
    """Create the automatic reversal of an approved journal, if its next period is open and it has none yet.

    It runs in a savepoint, so a reversal that can't be created (its template retired, say) never undoes the
    approval that triggered it. The failure is recorded in the history, where the exception report finds it."""
    if not row["auto_reverse"] or row["status"] not in store.APPROVED_OR_LATER:  # posted before the period opened
        return None
    if tx.execute(_LIVE_REVERSAL, {"id": row["journal_id"]}).first() is not None:
        return None
    period = tx.execute(_PERIOD_SHARE, {"e": row["entity_id"], "p": next_period(row["period"])}).mappings().first()
    if period is None or period["status"] not in store.TAKES_AUTOMATED:
        return None
    savepoint = tx.begin_nested()
    try:
        created = create(
            tx,
            setup,
            _reversal_draft(tx, row, period["starts_on"], "Automatic reversal of"),
            created_by=row["created_by"],
            actor=store.SYSTEM,
            request_id=request_id,
            source="reversal",
            reverses=row["journal_id"],
            auto_reverse=False,
        )
        frozen = frozen_amounts(tx, setup, created, None)  # the plain flip of its original: the original's rate
        if may_bypass("reversal", frozen.debits, created["functional_currency"], setup.matrix):
            reason = "automatic reversal; the approval matrix lets reversals skip approval"
            created = bypass(tx, setup, created, reason, request_id, frozen=frozen)
        savepoint.commit()
    except Problem as exc:
        savepoint.rollback()
        store.history(tx, action="auto_reverse_failed", actor=store.SYSTEM, journal=row, reason=exc.title)
        return None
    return created
