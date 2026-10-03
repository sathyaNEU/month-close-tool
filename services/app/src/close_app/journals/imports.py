"""CSV import of journals for one entity (BlackLine's "Import from Excel"; an XLSX workbook arrives here as CSV).

One row per journal line. Rows with the same ``journal_key`` form one journal:

- **Required columns:** ``journal_key``, ``template_id``, ``accounting_date``, ``description``, ``account``,
  ``side`` (DR or CR) and ``amount`` (in major units, such as 1234.56).
- **Optional columns:** ``currency`` (Stage 7c; empty means the entity's functional currency), ``line_description``,
  ``header:<column>``, ``dim:<dimension>`` and ``value:<column>``.
- **Journal-level values** (template, date, description, currency, header) come from a journal's first row. A later
  row may repeat them or leave them empty.
- **Amounts** are read with the journal currency's own decimals (none for JPY, three for KWD).
- **All or nothing.** Every journal is checked before any is created, and problems come back with CSV row numbers
  (the header is row 1). A rejected file is still recorded, with its SHA-256 and its issues.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, text

from close_app import audit
from close_app.identity import Principal
from close_app.journals import store, workflow
from close_app.journals.setup import Setup
from close_contracts.config.journal_workflow import Entity
from close_contracts.journals import JournalDraft, journal_issues
from close_contracts.money import CURRENCY_EXPONENTS

MAX_BYTES: Final = 5 * 1024 * 1024
MAX_ROWS: Final = 50_000
REQUIRED: Final = ("journal_key", "template_id", "accounting_date", "description", "account", "side", "amount")
JOURNAL_LEVEL: Final = ("template_id", "accounting_date", "description")
OPTIONAL: Final = ("currency", "line_description")
_PREFIXED: Final = re.compile(r"^(header|dim|value):([a-z][a-z0-9_]{0,40})$")
_PERIOD: Final = text("SELECT status FROM accounting_periods WHERE entity_id = :e AND period = :p")
_RECORD: Final = text(
    "INSERT INTO journal_imports (import_id, entity_id, sha256, row_count, journal_count, status, issues, "
    "imported_by) VALUES (:id, :e, :sha, :rows, :n, :st, CAST(:issues AS jsonb), :by)"
)


@dataclass
class _Group:
    first_row: int
    fields: dict[str, str]
    header: dict[str, str]
    lines: list[dict[str, Any]] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)


def _issue(row: int, where: str, code: str, message: str) -> dict[str, str]:
    return {"code": code, "path": f"/rows/{row}/{where}".rstrip("/"), "message": message}


def _minor(amount: str, units: int) -> int | None:
    try:
        value = Decimal(amount.strip())
    except InvalidOperation:
        return None
    scaled = value.scaleb(units)
    if not value.is_finite() or value <= 0 or scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def _groups(reader: csv.DictReader[str], entity: Entity, issues: list[dict[str, str]]) -> dict[str, _Group]:
    groups: dict[str, _Group] = {}
    for n, raw in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in raw.items() if k is not None}
        key = row["journal_key"]
        if not key:
            issues.append(_issue(n, "journal_key", "missing_key", "every row needs a journal_key"))
            continue
        header = {m.group(2): v for k, v in row.items() if (m := _PREFIXED.match(k)) and m.group(1) == "header" and v}
        group = groups.get(key)
        level = {f: row.get(f, "") for f in (*JOURNAL_LEVEL, "currency")}
        if group is None:
            group = groups[key] = _Group(n, level, header)
        else:
            issues.extend(
                _issue(n, f, "inconsistent_journal", f"{f} differs from row {group.first_row}")
                for f, value in level.items()
                if value and value != group.fields[f]
            )
            for k, v in header.items():
                if group.header.get(k, v) != v:
                    issues.append(
                        _issue(n, f"header:{k}", "inconsistent_journal", f"differs from row {group.first_row}")
                    )
                group.header.setdefault(k, v)
        currency = group.fields["currency"] or entity.functional_currency
        units = CURRENCY_EXPONENTS.get(currency)
        if units is None:
            issues.append(_issue(n, "currency", "unknown_currency", f"{currency} is not a currency the platform knows"))
            continue
        amount = _minor(row["amount"], units)
        if amount is None:
            message = f"{row['amount']!r} isn't a positive {currency} amount with at most {units} decimals"
            issues.append(_issue(n, "amount", "invalid_amount", message))
            continue
        group.lines.append(
            {
                "account": row["account"],
                "side": row["side"].upper(),
                "amount_minor": amount,
                "description": row.get("line_description", ""),
                "dimensions": {
                    m.group(2): v for k, v in row.items() if (m := _PREFIXED.match(k)) and m.group(1) == "dim" and v
                },
                "values": {
                    m.group(2): v for k, v in row.items() if (m := _PREFIXED.match(k)) and m.group(1) == "value" and v
                },
            }
        )
        group.rows.append(n)
    return groups


def _row_path(group: _Group, path: str) -> tuple[int, str]:
    """Map a journal issue's path to a CSV row: a line's issue to that line's row, the rest to the first row."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "lines" and parts[1].isdigit() and int(parts[1]) < len(group.rows):
        return group.rows[int(parts[1])], "/".join(parts[2:])
    if parts[0] == "header" and len(parts) > 1:
        return group.first_row, f"header:{parts[1]}"
    return group.first_row, "/".join(parts)


def parse(
    body: bytes, setup: Setup, entity: Entity, tx: Connection
) -> tuple[list[JournalDraft], int, list[dict[str, str]]]:
    """The drafts in ``body``, the number of data rows, and every problem found (empty when all are valid)."""
    issues: list[dict[str, str]] = []
    try:
        text_body = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], 0, [{"code": "not_utf8", "path": "/", "message": "the file must be UTF-8 text"}]
    reader = csv.DictReader(io.StringIO(text_body, newline=""))
    columns = reader.fieldnames or []
    missing = [c for c in REQUIRED if c not in columns]
    unknown = [c for c in columns if c not in REQUIRED and c not in OPTIONAL and not _PREFIXED.match(c)]
    if missing or unknown:
        message = f"missing columns: {missing}; unknown columns: {unknown}"
        return [], 0, [{"code": "bad_columns", "path": "/rows/1", "message": message}]
    groups = _groups(reader, entity, issues)
    rows = reader.line_num - 1
    if rows > MAX_ROWS:
        return [], rows, [{"code": "too_many_rows", "path": "/", "message": f"at most {MAX_ROWS} rows"}]
    drafts: list[JournalDraft] = []
    for group in groups.values():
        try:
            draft = JournalDraft.model_validate(
                {
                    "entity_id": entity.entity_id,
                    "template_id": group.fields["template_id"],
                    "accounting_date": group.fields["accounting_date"],
                    "description": group.fields["description"],
                    "header": group.header,
                    "lines": group.lines,
                    "currency": None
                    if group.fields["currency"] in ("", entity.functional_currency)
                    else group.fields["currency"],
                }
            )
        except ValidationError as exc:
            for e in exc.errors():
                n, where = _row_path(group, "/".join(map(str, e["loc"])))
                issues.append(_issue(n, where, str(e["type"]), str(e["msg"])))
            continue
        template = setup.templates.get(draft.template_id)
        if template is None:
            issues.append(
                _issue(group.first_row, "template_id", "unknown_template", f"{draft.template_id} is not active")
            )
            continue
        for i in journal_issues(
            draft,
            template=template[0],
            chart=setup.chart,
            dimensions=setup.dimensions,
            currency=entity.functional_currency,
        ):
            n, where = _row_path(group, i.path)
            issues.append(_issue(n, where, i.code, i.message))
        status = tx.execute(_PERIOD, {"e": entity.entity_id, "p": store.period_of(draft.accounting_date)}).scalar()
        if status not in store.TAKES_MANUAL:
            message = f"period {store.period_of(draft.accounting_date)} is {status or 'not opened'}"
            issues.append(_issue(group.first_row, "accounting_date", "period_not_open", message))
        drafts.append(draft)
    return drafts, rows, issues


def run(
    tx: Connection,
    setup: Setup,
    entity: Entity,
    body: bytes,
    *,
    p: Principal,
    request_id: str | None,
    digest: str | None = None,
    refused: list[dict[str, str]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Create every journal in the file, or none. Returns the HTTP status and body (201, or 422 with the issues).

    ``digest`` is the uploaded file's SHA-256 when ``body`` was converted from it (an XLSX workbook), and
    ``refused`` holds the issues of a file that couldn't be converted at all; either way the import is recorded."""
    digest = digest or hashlib.sha256(body).hexdigest()
    import_id = uuid.uuid4()
    drafts: list[JournalDraft] = []
    rows = 0
    if refused is not None:
        issues = refused
    elif len(body) > MAX_BYTES:
        issues = [{"code": "too_large", "path": "/", "message": f"at most {MAX_BYTES} bytes"}]
    else:
        drafts, rows, issues = parse(body, setup, entity, tx)
    status = "rejected" if issues or not drafts else "created"
    if not issues and not drafts:
        issues = [{"code": "empty", "path": "/", "message": "the file has no journal lines"}]
    tx.execute(
        _RECORD,
        {
            "id": import_id,
            "e": entity.entity_id,
            "sha": digest,
            "rows": rows,
            "n": 0 if issues else len(drafts),
            "st": status,
            "issues": json.dumps(issues),
            "by": p.user_id,
        },
    )
    created = []
    if not issues:
        for draft in drafts:
            row = workflow.create(
                tx, setup, draft, created_by=p.user_id, actor=p.user_id, request_id=request_id, source="import",
                import_id=import_id,
            )  # fmt: skip
            created.append({"journal_id": str(row["journal_id"]), "number": row["number"], "etag": store.etag(row)})
    audit.append(
        tx,
        audit.Event(
            p.user_id,
            "journal.import",
            entity_ids=(entity.entity_id,),
            target=f"journal_import:{import_id}",
            payload={"sha256": digest, "rows": rows, "status": status, "journals": len(created), "issues": len(issues)},
        ),
        request_id,
    )
    body_out: dict[str, Any] = {
        "import_id": str(import_id),
        "status": status,
        "sha256": digest,
        "rows": rows,
        "journals": created,
    }
    if issues:
        body_out |= {
            "type": "urn:close:problem:validation-failed",
            "title": "The file was not imported",
            "issues": issues,
        }
        return 422, body_out
    return 201, body_out


def load(conn: Connection, import_id: UUID) -> dict[str, Any] | None:
    row = (
        conn.execute(text("SELECT * FROM journal_imports WHERE import_id = :id"), {"id": import_id}).mappings().first()
    )
    if row is None:
        return None
    out = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in row.items()}
    out["import_id"] = str(row["import_id"])
    return out
