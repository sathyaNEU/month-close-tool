"""Ledger invariants for the sample rules: every balance ties to its source of truth (Gate 6).

Each check computes its expectation directly from the landing tables, never from events or rules, so a mistake
in event mapping or posting shows up as a difference. Injected breaks appear as named reconciling items, not as
silent differences:

- **Every event balances**, in the transaction currency and, after its rounding line, in the functional currency.
- **Buyer deposits (2020)** are orders captured but not yet at their program's trigger nor refunded before it.
- **Seller payables (2010)** are the payable ledger, less its injected duplicates, less completed payouts.
- **Payout clearing (1130)** is the funded payouts that did not complete: returned and unclaimed items.
- **Sales tax payable (2110)** is the tax engine's committed tax less remittances, less the tax on
  failed-verification refunds. The engine keeps those documents committed; the sample rules reverse their tax.
- **Carrier accrual (2410)** is the labels not yet invoiced.
- **PSP clearing (1110)** ties to each provider's unpaid balance. The difference is settlements in another
  currency (booked at the order's presentment amount, awaiting realized FX in Stage 5) and rounding breaks.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from close_contracts.config.accounting_policy import AccountingPolicy
from close_engine.reference.journal.posting import Line

Row = Mapping[str, Any]
Check = tuple[bool, str]
STAGES = {"verification_passed": "verification_passed", "shipped_to_buyer": "shipped", "delivered": "delivered",
          "return_window_closed": "return_window_closed"}  # fmt: skip


def _minor(text: object) -> int:
    return int(Decimal(str(text)).scaleb(2)) if text not in (None, "") else 0


def _net(lines: Iterable[Line], account: str, *, functional: bool, key: Any) -> dict[Any, int]:
    out: dict[Any, int] = defaultdict(int)
    for ln in lines:
        if ln.account == account:
            amount = ln.functional_amount_minor if functional else ln.amount_minor
            out[key(ln)] += amount if ln.side == "DR" else -amount
    return {k: v for k, v in out.items() if v}


def _same(name: str, gl: Mapping[Any, int], expected: Mapping[Any, int], extra: str = "") -> Check:
    gl = {k: v for k, v in gl.items() if v}
    expected = {k: v for k, v in expected.items() if v}
    diffs = {
        k: (gl.get(k, 0), expected.get(k, 0)) for k in set(gl) | set(expected) if gl.get(k, 0) != expected.get(k, 0)
    }
    detail = (
        f"{name}: {len(expected)} balances tie"
        if not diffs
        else f"{name}: {len(diffs)} differ, e.g. {sorted(diffs.items())[:2]}"
    )
    return not diffs, detail + extra


def balanced_events(lines: Iterable[Line]) -> Check:
    tx: dict[tuple[str, int], int] = defaultdict(int)
    fn: dict[tuple[str, int], int] = defaultdict(int)
    for ln in lines:
        sign = 1 if ln.side == "DR" else -1
        tx[(ln.event_id, ln.event_version)] += sign * ln.amount_minor
        fn[(ln.event_id, ln.event_version)] += sign * ln.functional_amount_minor
    bad = [k for k in tx if tx[k] or fn[k]]
    return not bad, f"{len(tx)} event versions balance in both currencies" if not bad else f"{len(bad)} unbalanced"


class _Orders:
    def __init__(self, tables: Mapping[str, Sequence[Row]], policy: AccountingPolicy) -> None:
        self.orders = {str(o["order_id"]): o for o in tables.get("landing.orders", ())}
        self.functional = {str(e["entity_id"]): str(e["functional_currency"]) for e in tables.get("ref.entities", ())}
        triggers = {p.program.value: p.trigger.value for p in policy.programs}
        self.events: dict[str, list[Row]] = defaultdict(list)
        for e in tables.get("landing.order_events", ()):
            self.events[str(e["order_id"])].append(e)
        self.triggered = set()
        for order_id, o in self.orders.items():
            trigger = triggers.get(str(o["program"]))
            stages = {STAGES.get(str(e["event_type"])) for e in self.events[order_id]}
            if trigger == "payment" or trigger in stages:
                self.triggered.add(order_id)
        self.full_refund_before = {
            order_id
            for order_id, evs in self.events.items()
            if order_id not in self.triggered
            and any(e["event_type"] in ("refund_cancel", "refund_verification_failed") for e in evs)
        }

    def domestic(self, order_id: str) -> bool:
        o = self.orders[order_id]
        return str(o["currency"]) == self.functional[str(o["entity_id"])]


def buyer_deposits(lines: Sequence[Line], o: _Orders) -> Check:
    expected: dict[tuple[str, str], int] = defaultdict(int)
    for order_id, row in o.orders.items():
        if order_id not in o.triggered and order_id not in o.full_refund_before:
            deposit = row["card_amount_minor"] + row["store_credit_minor"] - row["tax_minor"]
            expected[(str(row["entity_id"]), str(row["currency"]))] -= deposit
    gl = _net(lines, "2020", functional=False, key=lambda ln: (ln.entity_id, ln.currency))
    return _same("buyer deposits", gl, expected)


def seller_payables(tables: Mapping[str, Sequence[Row]], lines: Sequence[Line]) -> Check:
    expected: dict[str, int] = defaultdict(int)
    duplicates = 0
    for p in tables.get("landing.seller_payables", ()):
        if str(p["payable_id"]).endswith("-dup"):
            duplicates += 1
            continue
        expected[str(p["entity_id"])] -= int(p["amount_minor"])
    for item in tables.get("landing.payout_items", ()):
        if item["status"] == "COMPLETED":
            expected[str(item["entity_id"])] += _minor(item["amount"])
    gl = _net(lines, "2010", functional=True, key=lambda ln: ln.entity_id)
    return _same("seller payables", gl, expected, f"; {duplicates} injected duplicate payables are reconciling items")


def payout_clearing(tables: Mapping[str, Sequence[Row]], lines: Sequence[Line]) -> Check:
    expected: dict[str, int] = defaultdict(int)
    for item in tables.get("landing.payout_items", ()):
        if item["status"] != "COMPLETED":
            expected[str(item["entity_id"])] += _minor(item["amount"])
    gl = _net(lines, "1130", functional=True, key=lambda ln: ln.entity_id)
    return _same("payout clearing = returned and unclaimed payouts", gl, expected)


def sales_tax(tables: Mapping[str, Sequence[Row]], lines: Sequence[Line], o: _Orders) -> Check:
    expected: dict[tuple[str, str], int] = defaultdict(int)
    failed_verification = 0
    for doc in tables.get("landing.tax_documents", ()):
        # A document shows only its final status: a voided one was committed until its order was cancelled.
        order_id = str(doc["document_code"])
        cancelled = any(e["event_type"] == "refund_cancel" for e in o.events.get(order_id, ()))
        if doc["status"] == "Committed" or (order_id in o.orders and not cancelled):
            expected[(str(doc["entity_id"]), str(doc["currency"]))] -= _minor(doc["total_tax"])
    for r in tables.get("landing.tax_remittances", ()):
        expected[(str(r["entity_id"]), str(r["currency"]))] += int(r["amount_minor"])
    for order_id in o.full_refund_before:
        if any(e["event_type"] == "refund_verification_failed" for e in o.events[order_id]):
            row = o.orders[order_id]
            expected[(str(row["entity_id"]), str(row["currency"]))] += int(row["tax_minor"])
            failed_verification += 1
    gl = _net(lines, "2110", functional=False, key=lambda ln: (ln.entity_id, ln.currency))
    return _same("sales tax payable", gl, expected, f"; {failed_verification} failed-verification refunds reconcile")


def carrier_accrual(tables: Mapping[str, Sequence[Row]], lines: Sequence[Line]) -> Check:
    invoiced = {
        str(line["tracking_number"])
        for line in tables.get("landing.carrier_invoice_lines", ())
        if line["charge_type"] == "transportation"
    }
    expected: dict[str, int] = defaultdict(int)
    open_labels = 0
    for label in tables.get("landing.shipping_labels", ()):
        if str(label["tracking_number"]) not in invoiced:
            expected[str(label["entity_id"])] -= int(label["quoted_minor"])
            open_labels += 1
    gl = _net(lines, "2410", functional=True, key=lambda ln: ln.entity_id)
    return _same("carrier accrual = labels not yet invoiced", gl, expected, f" ({open_labels} labels)")


def _psp_rows(tables: Mapping[str, Sequence[Row]]) -> list[tuple[str, str, str | None, int, str]]:
    """(entity, psp, order id, settled amount in the functional currency, kind) for every provider transaction,
    and the payouts as kind "payout"."""
    out = []
    for r in tables.get("landing.stripe_balance_transactions", ()):
        if r["type"] == "payout":
            continue
        entity = str(r["account_id"]).removeprefix("acct_1Platform")
        kind = "capture" if r["type"] == "charge" else ("refund" if r["type"] == "refund" else "other")
        out.append((entity, "stripe", r["order_ref"], int(r["amount"]), kind))
        out.append((entity, "stripe", r["order_ref"], -int(r["fee"]), "fee"))
    out.extend(
        (str(r["account_id"]).removeprefix("acct_1Platform"), "stripe", None, -int(r["amount"]), "payout")
        for r in tables.get("landing.stripe_payouts", ())
    )
    for r in tables.get("landing.adyen_settlement_details", ()):
        entity = str(r["merchant_account"]).removeprefix("Platform")
        net = _minor(r["net_credit"]) - _minor(r["net_debit"])
        if r["type"] == "MerchantPayout":
            out.append((entity, "adyen", None, net, "payout"))
            continue
        fee = _minor(r["commission"])
        kind = {"Settled": "capture", "Refunded": "refund"}.get(str(r["type"]), "other")
        out.append((entity, "adyen", r["merchant_reference"], net + fee, kind))
        out.append((entity, "adyen", r["merchant_reference"], -fee, "fee"))
    return out


def psp_clearing(tables: Mapping[str, Sequence[Row]], lines: Sequence[Line], o: _Orders) -> Check:
    """In each entity's own currency: the GL equals the provider's unpaid balance, less every settled capture and
    card refund, plus the domestic orders' card amounts and card refunds as the rules booked them."""
    unpaid: dict[tuple[str, str], int] = defaultdict(int)
    settled_orders: dict[tuple[str, str], int] = defaultdict(int)
    for entity, psp, order_id, amount, kind in _psp_rows(tables):
        unpaid[(entity, psp)] += amount
        if kind in ("capture", "refund") and order_id is not None:
            settled_orders[(entity, psp)] += amount
    booked: dict[tuple[str, str], int] = defaultdict(int)
    foreign: dict[tuple[str, str, str], int] = defaultdict(int)
    for order_id, row in o.orders.items():
        refunds = sum(
            int(e["amount_minor"])
            for e in o.events[order_id]
            if str(e["event_type"]).startswith("refund_") and e["refund_method"] == "card"
        )
        amount = row["card_amount_minor"] - refunds
        key = (str(row["entity_id"]), str(row["psp"]))
        if o.domestic(order_id):
            booked[key] += amount
        else:
            foreign[(*key, str(row["currency"]))] += amount
    expected = {k: unpaid[k] - settled_orders[k] + booked[k] for k in set(unpaid) | set(booked)}
    gl = _net(
        (ln for ln in lines if ln.currency == o.functional[ln.entity_id]),
        "1110",
        functional=False,
        key=lambda ln: (ln.entity_id, dict(ln.dimensions).get("psp", "")),
    )
    ok, detail = _same("PSP clearing (own currency)", gl, expected)
    gl_foreign = _net(
        (ln for ln in lines if ln.currency != o.functional[ln.entity_id]),
        "1110",
        functional=False,
        key=lambda ln: (ln.entity_id, dict(ln.dimensions).get("psp", ""), ln.currency),
    )
    ok2, detail2 = _same("PSP clearing (other currencies, awaiting realized FX)", gl_foreign, foreign)
    return ok and ok2, f"{detail}; {detail2}"


def check_all(tables: Mapping[str, Sequence[Row]], lines: Sequence[Line], policy: AccountingPolicy) -> dict[str, Check]:
    """Every invariant, for lines booked under ``policy`` from ``tables``."""
    o = _Orders(tables, policy)
    return {
        "balanced_events": balanced_events(lines),
        "buyer_deposits": buyer_deposits(lines, o),
        "seller_payables": seller_payables(tables, lines),
        "payout_clearing": payout_clearing(tables, lines),
        "sales_tax": sales_tax(tables, lines, o),
        "carrier_accrual": carrier_accrual(tables, lines),
        "psp_clearing": psp_clearing(tables, lines, o),
    }


# The date that places each landing row in time, for "as of" snapshots.
AS_OF_COLUMNS: dict[str, str] = {
    "landing.orders": "placed_at",
    "landing.order_events": "occurred_at",
    "landing.stripe_balance_transactions": "created",
    "landing.stripe_payouts": "created",
    "landing.adyen_settlement_details": "creation_date",
    "landing.psp_fee_invoices": "invoice_date",
    "landing.disputes": "opened_at",
    "landing.seller_payables": "created_at",
    "landing.payout_items": "created_at",
    "landing.payout_funding": "value_date",
    "landing.shipping_labels": "ship_date",
    "landing.carrier_invoice_lines": "invoice_date",
    "landing.tax_documents": "document_date",
    "landing.tax_remittances": "paid_date",
    "landing.stored_value_ledger": "occurred_at",
}


def as_of(tables: Mapping[str, Sequence[Row]], cutoff: Any) -> dict[str, list[Row]]:
    """The landing tables as they stood at the end of ``cutoff`` (reference tables are kept whole)."""
    from datetime import datetime

    def day(value: Any) -> Any:
        return value.date() if isinstance(value, datetime) else value

    out: dict[str, list[Row]] = {}
    for table, rows in tables.items():
        column = AS_OF_COLUMNS.get(table)
        out[table] = list(rows) if column is None else [r for r in rows if day(r[column]) <= cutoff]
    return out
