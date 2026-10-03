"""Self-consistency checks of the answer key (Gate 2b: 100% of them must pass).

The checks read only the generated rows and the answer key, never the generator's internal plan, so they
test the key the way Stage 3 will use it:

1. every member row exists under its identity key;
2. every group has the shape its template needs (one order, one payout, one label, ...);
3. no row belongs to two groups of the same kind;
4. every ``auto`` group balances by its template's rule;
5. every group that names a break has a member recorded in the injected-break log;
6. an independent scan for captures that share currency, amount and date with no reference to tell them
   apart, which a keyless rule could not match safely; any such capture in an ``auto`` group fails.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from close_contracts.default_sources import IDENTITY_KEYS, source_key
from close_datagen.landing import Builder

Report = dict[str, dict[str, Any]]


def _minor(text: object) -> int:
    return int(Decimal(str(text)).scaleb(2)) if text not in (None, "") else 0


def _index(b: Builder) -> dict[tuple[str, str], dict[str, Any]]:
    out = {}
    for table, keys in IDENTITY_KEYS.items():
        for row in b.rows.get(table, []):
            out[(table, source_key([row[k] for k in keys]))] = row
    return out


def _result(checked: int, failures: list[str]) -> dict[str, Any]:
    return {"checked": checked, "failed": len(failures), "examples": failures[:5]}


def _bank_amount(row: dict[str, Any]) -> int:
    text = str(row["amount_text"])
    return int(text) if row["format"] == "bai2" else _minor(text.replace(",", "."))


def _balance(
    template: int, group_key: str, members: list[dict[str, Any]], rows: dict[tuple[str, str], dict[str, Any]]
) -> bool:
    by_side: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in members:
        by_side[m["side"]].append(rows[(m["source_table"], m["source_key"])] | {"_table": m["source_table"]})
    a, b, c = by_side.get("a", []), by_side.get("b", []), by_side.get("c", [])
    if template == 1:
        captured = sum(
            int(r["customer_facing_amount"])
            if r["_table"].endswith("stripe_balance_transactions")
            else _minor(r["gross_credit"])
            for r in b
        )
        return int(a[0]["card_amount_minor"]) == captured
    if template == 2:
        if a[0]["_table"].endswith("stripe_payouts"):
            return int(a[0]["amount"]) == sum(int(r["net"]) for r in b)
        return _minor(a[0]["net_debit"]) == sum(_minor(r["net_credit"]) - _minor(r["net_debit"]) for r in b)
    if template == 3:
        payout = int(a[0]["amount"]) if a[0]["_table"].endswith("stripe_payouts") else _minor(a[0]["net_debit"])
        return payout == _bank_amount(b[0]) and b[0]["credit_debit"] == "C"
    if template == 6:
        return _minor(a[0]["amount"]) == sum(int(r["amount_minor"]) for r in b)
    if template == 9:
        return int(a[0]["quoted_minor"]) == sum(int(r["billed_minor"]) for r in b)
    if template == 10:
        if c:
            return int(c[0]["amount_minor"]) == sum(_minor(r["total_tax"]) for r in b)
        return int(a[0]["tax_minor"]) == _minor(b[0]["total_tax"])
    raise ValueError(f"no balance rule for template {template} ({group_key})")


_SHAPES = {
    1: {"a": (1, 1), "b": (1, 9)},
    2: {"a": (1, 1), "b": (1, 10**6)},
    3: {"a": (1, 1), "b": (1, 1)},
    9: {"a": (1, 1), "b": (1, 9)},
}


def check(b: Builder) -> Report:
    rows = _index(b)
    group_list = list(b.group_rows())
    member_list = list(b.member_rows())
    members: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for m in member_list:
        members[(m["template"], m["group_key"])].append(m)

    missing = [
        f"{m['group_key']}: {m['source_table']} {m['source_key']}"
        for m in member_list
        if (m["source_table"], m["source_key"]) not in rows
    ]

    shape_failures = []
    for g in group_list:
        sides = Counter(m["side"] for m in members[(g["template"], g["group_key"])])
        if g["template"] == 9 and g["expected_outcome"] == "open":  # a carrier correction line on its own
            bad = dict(sides) != {"b": 1}
        elif g["template"] in _SHAPES:
            wanted = _SHAPES[g["template"]]
            bad = any(not lo <= sides.get(side, 0) <= hi for side, (lo, hi) in wanted.items())
        elif g["template"] == 6:
            bad = sides.get("a", 0) > 1 or sides.get("b", 0) < 1
        else:  # template 10: an order with its document, or a remittance with its documents
            bad = not (
                (sides.get("a") == 1 and sides.get("b") == 1) or (sides.get("c") == 1 and sides.get("b", 0) >= 1)
            )
        if bad:
            shape_failures.append(f"{g['group_key']}: {dict(sides)}")

    kind = {
        (g["template"], g["group_key"]): f"{g['template']}{'r' if str(g['group_key']).startswith('T10-REM') else ''}"
        for g in group_list
    }
    seen = Counter((kind[(m["template"], m["group_key"])], m["source_table"], m["source_key"]) for m in member_list)
    duplicates = [f"{k[0]} {k[1]} {k[2]}" for k, n in seen.items() if n > 1]

    unbalanced = []
    auto = [g for g in group_list if g["expected_outcome"] == "auto"]
    for g in auto:
        group_members = members[(g["template"], g["group_key"])]
        if not missing and not _balance(g["template"], g["group_key"], group_members, rows):
            unbalanced.append(str(g["group_key"]))

    logged = {(r["break_type"], r["source_key"]) for r in b.rows.get("ops.injected_breaks", [])}
    unlogged = []
    labelled = [g for g in group_list if g["break_type"]]
    for g in labelled:
        keys = {m["source_key"] for m in members[(g["template"], g["group_key"])]}
        if not any((g["break_type"], k) in logged for k in keys):
            unlogged.append(f"{g['group_key']}: {g['break_type']}")

    collisions = []
    captures: dict[tuple[str, int, str], list[str]] = defaultdict(list)
    for row in b.rows.get("landing.stripe_balance_transactions", []):
        if row["type"] == "charge" and not row["order_ref"] and not row["payment_intent_id"]:
            captures[(str(row["currency"]), int(row["amount"]), f"{row['created']:%Y-%m-%d}")].append(str(row["id"]))
    for row in b.rows.get("landing.adyen_settlement_details", []):
        if row["type"] == "Settled" and not row["merchant_reference"] and not row["psp_reference"]:
            captures[
                (str(row["gross_currency"]), _minor(row["gross_credit"]), f"{row['creation_date']:%Y-%m-%d}")
            ].append(str(row["modification_reference"]))
    ambiguous = {k for ids in captures.values() if len(ids) > 1 for k in ids}
    auto_members = {m["source_key"] for g in auto for m in members[(g["template"], g["group_key"])]}
    collisions = sorted(k for k in ambiguous if any(k in key for key in auto_members))

    return {
        "members_exist": _result(len(member_list), missing),
        "group_shapes": _result(len(group_list), shape_failures),
        "unique_membership": _result(len(seen), duplicates),
        "auto_groups_balance": _result(len(auto), unbalanced),
        "breaks_are_logged": _result(len(labelled), unlogged),
        "keyless_collisions_not_auto": {**_result(len(captures), collisions), "ambiguous_captures": len(ambiguous)},
    }


def passed(report: Report) -> bool:
    return all(r["failed"] == 0 for r in report.values())
