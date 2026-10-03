"""Journal rows: loading and locking, gapless numbers, line revisions, the history, audit events and JSON."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from close_app import audit
from close_contracts.journals import Conversion, JournalDraft, JournalLineIn, content_hash

FIELDS: Final = (
    "journal_id", "entity_id", "number", "template_id", "template_version", "period", "accounting_date", "currency",
    "description", "header", "status", "source", "auto_reverse", "revision", "content_hash", "total_debit_minor",
    "total_credit_minor", "line_count", "batch_id", "import_id", "recurring_id", "recurring_period",
    "reverses_journal_id", "created_by", "created_at", "submission", "submitted_by", "submitted_at", "matrix_id",
    "matrix_version", "routing", "risk_score", "risk_version", "risk_factors", "required_levels", "current_level",
    "level_since", "approved_at", "bypass_reason", "cancelled_by", "cancelled_at", "posting_run_id", "posted_at",
    "posted_version", "erp_document_id", "erp_confirmed_at", "erp_confirmed_by", "functional_currency",
    "functional_debit_minor", "functional_credit_minor", "ledger_line_count", "conversion_hash", "row_version",
)  # fmt: skip
SYSTEM: Final = "system:journal-workflow"  # the actor of automatic steps (reversals, recurring journals)
APPROVED_OR_LATER: Final = ("approved", "posting", "posted", "erp_confirmed")  # approved, and since (Stage 7b)
# Stage 8b: what a period's status lets in. A soft-closed period still takes people's entries (the close review's
# adjustments), but no automated ones (recurring journals, automatic reversals, the journaling engine).
TAKES_MANUAL: Final = frozenset({"open", "soft_closed"})
TAKES_AUTOMATED: Final = frozenset({"open"})

_ONE: Final = text("SELECT * FROM journals WHERE journal_id = :id")
_ONE_LOCKED: Final = text("SELECT * FROM journals WHERE journal_id = :id FOR UPDATE")
_LINES: Final = text("SELECT * FROM journal_lines WHERE journal_id = :id AND revision = :r ORDER BY line_no")
_APPROVALS: Final = text(
    "SELECT level, approver, on_behalf_of, content_hash, approved_at, batch_id FROM journal_approvals "
    "WHERE journal_id = :id AND submission = :s ORDER BY level"
)
_COUNTER_ROW: Final = text(
    "INSERT INTO journal_counters (entity_id, last_number) VALUES (:e, 0) ON CONFLICT (entity_id) DO NOTHING"
)
_NEXT: Final = text(
    "UPDATE journal_counters SET last_number = last_number + 1 WHERE entity_id = :e RETURNING last_number"
)
_INSERT: Final = text(
    "INSERT INTO journals (journal_id, entity_id, number, template_id, template_version, period, accounting_date, "
    "currency, functional_currency, description, header, status, source, auto_reverse, revision, content_hash, "
    "total_debit_minor, total_credit_minor, line_count, batch_id, import_id, recurring_id, recurring_period, "
    "reverses_journal_id, created_by) VALUES (:id, :e, :n, :t, :tv, :p, :d, :c, :fc, :desc, CAST(:h AS jsonb), "
    "'draft', :src, :ar, 1, :hash, :dr, :cr, :lc, :batch, :imp, :rec, :recp, :rev, :by)"
)
_CONVERSION: Final = text("SELECT * FROM journal_conversions WHERE journal_id = :id AND submission = :s")
_INSERT_CONVERSION: Final = text(
    "INSERT INTO journal_conversions (journal_id, submission, revision, currency, functional_currency, rate, "
    "rate_date, rate_type, rate_version, rate_source, rate_table_version, reused_from, detail, conversion_hash, "
    "functional_total_minor, rounding_side, rounding_minor, rounding_account) VALUES (:id, :s, :r, :c, :fc, :rate, "
    ":rd, :rt, :rv, :src, :tv, :reused, CAST(:detail AS jsonb), :hash, :total, :side, :rounding, :account)"
)
_LINE: Final = text(
    "INSERT INTO journal_lines (journal_id, revision, line_no, account, side, amount_minor, dimensions, description, "
    "column_values) VALUES (:id, :r, :n, :a, :s, :m, CAST(:dims AS jsonb), :desc, CAST(:vals AS jsonb))"
)
_HISTORY: Final = text(
    "INSERT INTO journal_history (journal_id, batch_id, action, from_status, to_status, submission, level, actor, "
    "on_behalf_of, reason) VALUES (:j, :b, :a, :f, :t, :s, :l, :by, :obo, :why)"
)


def period_of(day: date) -> str:
    return f"{day:%Y-%m}"


def load(tx: Connection, journal_id: str | UUID, *, lock: bool = False) -> RowMapping | None:
    try:
        key = UUID(str(journal_id))
    except ValueError:
        return None
    return tx.execute(_ONE_LOCKED if lock else _ONE, {"id": key}).mappings().first()


def reload(tx: Connection, row: RowMapping) -> RowMapping:
    new = load(tx, row["journal_id"])
    if new is None:  # written or locked in this transaction, so it cannot vanish
        raise RuntimeError(f"journal {row['journal_id']} disappeared")
    return new


def lines(tx: Connection, row: RowMapping) -> list[RowMapping]:
    return list(tx.execute(_LINES, {"id": row["journal_id"], "r": row["revision"]}).mappings())


def approvals(tx: Connection, row: RowMapping) -> list[RowMapping]:
    return list(tx.execute(_APPROVALS, {"id": row["journal_id"], "s": row["submission"]}).mappings())


def draft_of(tx: Connection, row: RowMapping) -> JournalDraft:
    """The journal's current content, as the draft it was saved from. A journal in its functional currency has no
    currency in its draft, so journals saved before Stage 7c rebuild to exactly the content their hash covers."""
    return JournalDraft(
        entity_id=row["entity_id"],
        template_id=row["template_id"],
        accounting_date=row["accounting_date"],
        description=row["description"],
        header=dict(row["header"]),
        currency=None if row["currency"] == row["functional_currency"] else row["currency"],
        lines=tuple(
            JournalLineIn(
                account=ln["account"],
                side=ln["side"],
                amount_minor=ln["amount_minor"],
                dimensions=dict(ln["dimensions"]),
                description=ln["description"],
                values=dict(ln["column_values"]),
            )
            for ln in lines(tx, row)
        ),
    )


def etag(row: RowMapping) -> str:
    return f'"{row["row_version"]}-{row["content_hash"][:16]}"'


def target(row: RowMapping) -> str:
    return f"journal:{row['entity_id']}:{row['number']}"


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def as_json(tx: Connection, row: RowMapping, *, detail: bool = True) -> dict[str, Any]:
    out = {name: _plain(row[name]) for name in FIELDS}
    out["etag"] = etag(row)
    if detail:
        out["lines"] = [
            {
                "line_no": ln["line_no"],
                "account": ln["account"],
                "side": ln["side"],
                "amount_minor": ln["amount_minor"],
                "dimensions": ln["dimensions"],
                "description": ln["description"],
                "values": ln["column_values"],
            }
            for ln in lines(tx, row)
        ]
        # A draft's earlier approvals no longer count (the history keeps them), so only a live submission shows any.
        live = row["status"] == "pending_approval" or row["status"] in APPROVED_OR_LATER
        out["approvals"] = [{k: _plain(v) for k, v in a.items()} for a in approvals(tx, row)] if live else []
        frozen = conversion(tx, row) if live and row["conversion_hash"] else None
        out["conversion"] = dict(frozen["detail"]) if frozen is not None else None
    return out


def conversion(tx: Connection, row: RowMapping) -> RowMapping | None:
    """The conversion the journal's current submission froze (a foreign-currency journal past draft)."""
    return tx.execute(_CONVERSION, {"id": row["journal_id"], "s": row["submission"]}).mappings().first()


def insert_conversion(tx: Connection, row: RowMapping, frozen: Conversion, *, submission: int) -> None:
    rate = frozen.rate
    tx.execute(
        _INSERT_CONVERSION,
        {
            "id": row["journal_id"],
            "s": submission,
            "r": row["revision"],
            "c": frozen.currency,
            "fc": frozen.functional_currency,
            "rate": rate.rate,
            "rd": rate.key.on,
            "rt": rate.key.rate_type,
            "rv": rate.version,
            "src": rate.source,
            "tv": rate.table_version,
            "reused": frozen.reused_from,
            "detail": json.dumps(frozen.as_json(), sort_keys=True),
            "hash": frozen.hash(),
            "total": frozen.functional_total,
            "side": frozen.rounding_side,
            "rounding": frozen.rounding_minor,
            "account": frozen.rounding_account if frozen.rounding_side else None,
        },
    )


def next_number(tx: Connection, entity_id: str) -> int:
    """The entity's next journal number: consecutive, because the counter row moves in this transaction."""
    tx.execute(_COUNTER_ROW, {"e": entity_id})
    return int(tx.execute(_NEXT, {"e": entity_id}).scalar_one())


def write_lines(tx: Connection, journal_id: UUID, revision: int, draft: JournalDraft) -> None:
    tx.execute(
        _LINE,
        [
            {
                "id": journal_id,
                "r": revision,
                "n": i,
                "a": ln.account,
                "s": ln.side,
                "m": ln.amount_minor,
                "dims": json.dumps(ln.dimensions, sort_keys=True),
                "desc": ln.description,
                "vals": json.dumps(ln.values, sort_keys=True),
            }
            for i, ln in enumerate(draft.lines, start=1)
        ],
    )


def insert(
    tx: Connection,
    draft: JournalDraft,
    *,
    template_version: int,
    functional_currency: str,
    auto_reverse: bool,
    source: str,
    created_by: str,
    batch_id: UUID | None = None,
    import_id: UUID | None = None,
    recurring: tuple[str, str] | None = None,
    reverses: UUID | None = None,
) -> RowMapping:
    journal_id = uuid.uuid4()
    tx.execute(
        _INSERT,
        {
            "id": journal_id,
            "e": draft.entity_id,
            "n": next_number(tx, draft.entity_id),
            "t": draft.template_id,
            "tv": template_version,
            "p": period_of(draft.accounting_date),
            "d": draft.accounting_date,
            "c": draft.currency or functional_currency,
            "fc": functional_currency,
            "desc": draft.description,
            "h": json.dumps(draft.header, sort_keys=True),
            "src": source,
            "ar": auto_reverse,
            "hash": content_hash(draft),
            "dr": draft.total_debits(),
            "cr": draft.total_credits(),
            "lc": len(draft.lines),
            "batch": batch_id,
            "imp": import_id,
            "rec": recurring[0] if recurring else None,
            "recp": recurring[1] if recurring else None,
            "rev": reverses,
            "by": created_by,
        },
    )
    write_lines(tx, journal_id, 1, draft)
    row = load(tx, journal_id)
    if row is None:
        raise RuntimeError("the journal just inserted is missing")
    return row


def history(
    tx: Connection,
    *,
    action: str,
    actor: str,
    journal: RowMapping | None = None,
    batch_id: UUID | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    submission: int = 0,
    level: int | None = None,
    on_behalf_of: str | None = None,
    reason: str | None = None,
) -> None:
    tx.execute(
        _HISTORY,
        {
            "j": journal["journal_id"] if journal is not None else None,
            "b": batch_id,
            "a": action,
            "f": from_status,
            "t": to_status,
            "s": submission,
            "l": level,
            "by": actor,
            "obo": on_behalf_of,
            "why": reason,
        },
    )


def audit_event(tx: Connection, actor: str, verb: str, row: RowMapping, request_id: str | None, **payload: Any) -> None:
    event = audit.Event(
        actor,
        f"journal.{verb}",
        entity_ids=(row["entity_id"],),
        target=target(row),
        payload={
            "journal_id": str(row["journal_id"]),
            "status": row["status"],
            "revision": row["revision"],
            "content_hash": row["content_hash"],
            **payload,
        },
    )
    audit.append(tx, event, request_id)
