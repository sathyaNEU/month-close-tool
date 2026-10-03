"""The ERP's trial balance, and the Delta-to-ERP tie-out (Stage 8a, ADR-0019).

D19 keeps its default: an ERP stays the book of record. So the close compares what the platform posted (the
Databricks GL) with what the ERP holds.

- **The extract's contract.**
  - **Format.** CSV with a header row ``account,balance``, one row per account. A balance has no thousands
    separator, and a row with more fields than the header (an unquoted ``1,234.56``) is refused.
  - **The balance** is the account's closing balance for the period, in the entity's functional currency, in major
    units with that currency's decimals (``1234.56`` for USD, ``1234`` for JPY). Debit balances are positive and
    credit balances negative.
  - **Accounts** are the ERP's codes, up to 32 characters. An account the platform doesn't know is kept, and the
    tie-out reports it.
- **All or nothing.** Every row is checked before anything is stored, and the problems name their CSV rows (the
  header is row 1). Every upload is kept by entity, period and version with its SHA-256, accepted or rejected.
  Uploads are insert-only and audited.
- **The tie-out** compares each account's closing balance in the Databricks GL with the latest accepted extract's,
  and lists every difference. That includes an account only one side has (``erp_only``, ``gl_only``). Nothing is
  smoothed over.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import Connection, RowMapping, text

from close_app import audit
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.identity import Principal
from close_app.ledger.balances import balances
from close_contracts.money import exponent

MAX_BYTES: Final = 5 * 1024 * 1024
MAX_ROWS: Final = 50_000
MAX_FIELD: Final = 40  # characters in one balance
_BIGINT_DIGITS: Final = 19  # 2**63 has 19 digits
COLUMNS: Final = ("account", "balance")
_ACCOUNT: Final = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_LOCK: Final = text("SELECT pg_advisory_xact_lock(hashtext('erp_trial_balance:' || :e || ':' || :p))")
_NEXT_VERSION: Final = text(
    "SELECT coalesce(max(version), 0) + 1 FROM erp_trial_balances WHERE entity_id = :e AND period = :p"
)
_INSERT: Final = text(
    "INSERT INTO erp_trial_balances (extract_id, entity_id, period, version, status, sha256, currency, line_count, "
    "issues, uploaded_by) VALUES (:id, :e, :p, :v, :st, :sha, :c, :n, CAST(:issues AS jsonb), :by)"
)
_LINE: Final = text("INSERT INTO erp_trial_balance_lines (extract_id, account, balance_minor) VALUES (:id, :a, :b)")
_LATEST: Final = text(
    "SELECT * FROM erp_trial_balances WHERE entity_id = :e AND period = :p AND status = 'accepted' "
    "ORDER BY version DESC LIMIT 1"
)
_LIST: Final = text("SELECT * FROM erp_trial_balances WHERE entity_id = :e AND period = :p ORDER BY version")
_LINES: Final = text("SELECT account, balance_minor FROM erp_trial_balance_lines WHERE extract_id = :id")


def _issue(row: int, code: str, message: str) -> dict[str, str]:
    return {"code": code, "path": f"/rows/{row}", "message": message}


def _minor(value: str, units: int) -> int | None:
    """``value`` in minor units: None unless it is a finite amount with at most ``units`` decimals (trailing zeros
    aside) that fits a BIGINT. Exact integer arithmetic on the digits, so no context rounds, overflows or underflows."""
    value = value.strip()
    if len(value) > MAX_FIELD:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    sign, digits, exponent = amount.as_tuple()
    places = list(digits)
    while places and places[-1] == 0 and int(exponent) < -units:  # trailing zeros past the currency's decimals
        places.pop()
        exponent = int(exponent) + 1
    shift = int(exponent) + units
    if places and (shift < 0 or len(places) + shift > _BIGINT_DIGITS):
        return None
    minor = int("".join(map(str, places)) or "0") * 10**shift if places else 0
    return None if minor >= 2**63 else -minor if sign else minor


def parse(body: bytes, currency: str) -> tuple[dict[str, int], list[dict[str, str]]]:
    """The balances in ``body`` by account, and every problem (empty when the file is valid)."""
    if len(body) > MAX_BYTES:
        return {}, [{"code": "too_large", "path": "/", "message": f"at most {MAX_BYTES} bytes"}]
    try:
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline=""))
    except UnicodeDecodeError:
        return {}, [{"code": "not_utf8", "path": "/", "message": "the file must be UTF-8 text"}]
    if tuple(reader.fieldnames or ()) != COLUMNS:
        message = f"the header must be exactly {','.join(COLUMNS)}, not {','.join(reader.fieldnames or [])}"
        return {}, [{"code": "bad_columns", "path": "/rows/1", "message": message}]
    units = exponent(currency)
    balances_: dict[str, int] = {}
    issues: list[dict[str, str]] = []
    for n, raw in enumerate(reader, start=2):
        if n - 1 > MAX_ROWS:
            return {}, [{"code": "too_many_rows", "path": "/", "message": f"at most {MAX_ROWS} rows"}]
        account, value = (raw.get("account") or "").strip(), raw.get("balance") or ""
        if None in raw:  # more fields than the header: an unquoted "1,234.56" would otherwise read as 1
            issues.append(
                _issue(
                    n, "extra_fields", "the row has more fields than the header (a balance has no thousands separator)"
                )
            )
            continue
        if not _ACCOUNT.fullmatch(account):
            issues.append(_issue(n, "invalid_account", f"{account!r} isn't an account code"))
            continue
        if account in balances_:
            issues.append(_issue(n, "duplicate_account", f"account {account} appears twice"))
            continue
        minor = _minor(value, units)
        if minor is None:
            message = f"{value!r} isn't a {currency} balance with at most {units} decimals"
            issues.append(_issue(n, "invalid_balance", message))
            continue
        balances_[account] = minor
    if not issues and not balances_:
        issues.append({"code": "empty", "path": "/", "message": "the file has no balances"})
    return balances_, issues


def extract_json(row: RowMapping) -> dict[str, Any]:
    return {
        "extract_id": str(row["extract_id"]),
        "entity_id": row["entity_id"],
        "period": row["period"],
        "version": row["version"],
        "status": row["status"],
        "sha256": row["sha256"],
        "currency": row["currency"],
        "line_count": row["line_count"],
        "issues": row["issues"],
        "uploaded_by": row["uploaded_by"],
        "uploaded_at": row["uploaded_at"].isoformat(),
    }


def upload(
    tx: Connection,
    entity_id: str,
    period: str,
    currency: str,
    body: bytes,
    *,
    digest: str,
    p: Principal,
    request_id: str | None,
) -> tuple[int, dict[str, Any]]:
    """Store an extract, accepted or rejected (201, or 422 with its issues)."""
    found, issues = parse(body, currency)
    tx.execute(_LOCK, {"e": entity_id, "p": period})
    version = int(tx.execute(_NEXT_VERSION, {"e": entity_id, "p": period}).scalar_one())
    extract_id = uuid.uuid4()
    status = "rejected" if issues else "accepted"
    tx.execute(
        _INSERT,
        {
            "id": extract_id,
            "e": entity_id,
            "p": period,
            "v": version,
            "st": status,
            "sha": digest,
            "c": currency,
            "n": len(found) if not issues else 0,
            "issues": json.dumps(issues),
            "by": p.user_id,
        },
    )
    if not issues:
        tx.execute(_LINE, [{"id": extract_id, "a": a, "b": b} for a, b in sorted(found.items())])
    audit.append(
        tx,
        audit.Event(
            p.user_id,
            "erp.trial_balance",
            entity_ids=(entity_id,),
            target=f"erp_trial_balance:{extract_id}",
            payload={"period": period, "version": version, "status": status, "sha256": digest, "issues": len(issues)},
        ),
        request_id,
    )
    row = (
        tx.execute(text("SELECT * FROM erp_trial_balances WHERE extract_id = :id"), {"id": extract_id}).mappings().one()
    )
    return (422 if issues else 201), extract_json(row)


def extracts(conn: Connection, entity_id: str, period: str) -> list[dict[str, Any]]:
    return [extract_json(r) for r in conn.execute(_LIST, {"e": entity_id, "p": period}).mappings()]


def tie_out(ctx: AppContext, p: Principal, entity_id: str, period: str) -> dict[str, Any]:
    with ctx.engine.connect() as conn:
        extract = conn.execute(_LATEST, {"e": entity_id, "p": period}).mappings().first()
        if extract is None:
            raise Problem(
                404, "No ERP trial balance", f"none has been accepted for {entity_id} {period}", kind="not-found"
            )
        erp = {str(r.account): int(r.balance_minor) for r in conn.execute(_LINES, {"id": extract["extract_id"]})}
    gl = {r["account"]: int(r["closing_minor"]) for r in balances(ctx, p, entity_id, period)["accounts"]}
    items = []
    for account in sorted(set(gl) | set(erp)):
        ours, theirs = gl.get(account), erp.get(account)
        if not ours and not theirs:
            continue  # zero on both sides, or zero on one and absent on the other
        if ours is None:
            status = "erp_only"
        elif theirs is None:
            status = "gl_only"
        else:
            status = "ties" if ours == theirs else "differs"
        items.append(
            {
                "account": account,
                "gl_closing_minor": ours or 0,
                "erp_balance_minor": theirs or 0,
                "difference_minor": (theirs or 0) - (ours or 0),
                "status": status,
            }
        )
    differences = [i for i in items if i["status"] != "ties"]
    return {
        "entity_id": entity_id,
        "period": period,
        "extract": {
            k: v for k, v in extract_json(extract).items() if k in ("extract_id", "version", "sha256", "uploaded_at")
        },
        "items": items,
        "differences": len(differences),
        "ties": not differences,
    }
