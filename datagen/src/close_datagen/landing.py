"""Expanding the order plan into the landing tables, the injected-break log and the answer key.

``Builder`` collects rows per landing table as plain dicts keyed by column name; ``close_datagen.writer``
turns them into typed Arrow tables using the schema registry. Part one here covers the order-level tables;
``close_datagen.settlement`` adds the provider, payout and bank tables that aggregate across orders.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from close_contracts.default_sources import source_key
from close_datagen.plan import Order
from close_datagen.rng import Stream
from close_datagen.world import CALENDARS, CARRIER, Entity, Seller

Row = dict[str, Any]


def at(day: date, seconds: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(seconds=seconds)


def business_days_after(entity: Entity, day: date, n: int) -> date:
    return CALENDARS[entity.calendar].add_business_days(day, n)


def decimal_text(minor: int, exponent: int = 2) -> str:
    return format(Decimal(minor).scaleb(-exponent), "f")


ANSWER_CHUNK = 200_000
GroupTuple = tuple[int, str, str, str | None, str, str]  # template, group key, outcome, break, entity, currency
MemberTuple = tuple[int, str, str, str, str]  # template, group key, side, source table, source key


class Builder:
    """Collects rows per table. With a ``sink``, a table can be flushed (handed to the sink and released) as
    soon as nothing later reads it, which keeps the ``medium`` profile's memory small; without one (unit
    tests), every row stays in memory. The answer key is kept as compact tuples."""

    def __init__(self, seed: int, run_id: str, sink: Callable[[str, list[Row]], None] | None = None) -> None:
        self.seed = seed
        self.run_id = run_id
        self.rows: dict[str, list[Row]] = defaultdict(list)
        self.groups: list[GroupTuple] = []
        self.members: list[MemberTuple] = []
        self.sink = sink
        self.written: dict[str, int] = defaultdict(int)
        self.answer_total = [0, 0]  # groups, members ever recorded (they are streamed out when there is a sink)

    def add(self, table: str, row: Row) -> Row:
        self.rows[table].append(row)
        return row

    def flush(self, *tables: str) -> None:
        if self.sink is None:
            return
        for table in tables:
            rows = self.rows.pop(table, [])
            if rows:
                self.sink(table, rows)
                self.written[table] += len(rows)

    def count(self, table: str) -> int:
        return self.written[table] + len(self.rows.get(table, []))

    def brk(self, break_type: str, table: str, key: str, detail: str = "") -> None:
        self.add(
            "ops.injected_breaks",
            {
                "run_id": self.run_id,
                "break_type": break_type,
                "source_table": table,
                "source_key": key,
                "detail": detail,
            },
        )

    def answer(
        self,
        template: int,
        group_key: str,
        *,
        outcome: str,
        entity: str,
        currency: str,
        members: list[tuple[str, str, str]],
        break_type: str | None = None,
    ) -> None:
        self.groups.append((template, group_key, outcome, break_type, entity, currency))
        self.members.extend((template, group_key, side, table, key) for side, table, key in members)
        self.answer_total[0] += 1
        self.answer_total[1] += len(members)
        if self.sink is not None and len(self.members) >= ANSWER_CHUNK:
            self.flush_answers()

    def flush_answers(self) -> None:
        """Hand the answer key collected so far to the sink (appended there) and release it."""
        if self.sink is None:
            return
        if self.groups:
            self.sink("ops.answer_key_groups", list(self.group_rows()))
        if self.members:
            self.sink("ops.answer_key_members", list(self.member_rows()))
        self.groups, self.members = [], []

    def group_rows(self) -> Iterator[Row]:
        for template, group_key, outcome, break_type, entity, currency in self.groups:
            yield {
                "run_id": self.run_id,
                "template": template,
                "group_key": group_key,
                "expected_outcome": outcome,
                "break_type": break_type,
                "entity_id": entity,
                "currency": currency,
            }

    def member_rows(self) -> Iterator[Row]:
        for template, group_key, side, table, key in self.members:
            yield {
                "run_id": self.run_id,
                "template": template,
                "group_key": group_key,
                "side": side,
                "source_table": table,
                "source_key": key,
            }


def payout_date(order: Order, created: date) -> date:
    """Sellers are paid in a daily batch on the next business day after the payable is created."""
    return business_days_after(order.entity, created, 1)


def transfer_group(seller: Seller, paid: date) -> str:
    return f"pmt-{seller.seller_id}-{paid:%Y%m%d}"


def add_sellers(b: Builder, pools: dict[str, list[Seller]], start: date) -> None:
    for pool in pools.values():
        for s in pool:
            b.add(
                "landing.sellers",
                {
                    "seller_id": s.seller_id,
                    "entity_id": s.entity_id,
                    "country": s.entity_id[:2].replace("EU", "NL"),
                    "level": s.level,
                    "fee_bps": s.fee_bps,
                    "payout_provider": s.payout_provider,
                    "created_at": at(start - timedelta(days=400)),
                },
            )


def _event(b: Builder, o: Order, kind: str, day: date, seconds: int, **extra: Any) -> None:
    b.add(
        "landing.order_events",
        {
            "event_id": f"E{o.order_id}-{kind}",
            "order_id": o.order_id,
            "entity_id": o.entity.entity_id,
            "event_type": kind,
            "occurred_at": at(day, seconds),
            "tracking_number": extra.get("tracking"),
            "amount_minor": extra.get("amount"),
            "refund_method": extra.get("method"),
        },
    )


def refunds(o: Order) -> list[tuple[str, date, int, str]]:
    """(kind, day, amount in presentment minor units, method) for every refund of an order."""
    out = []
    if o.cancelled:
        out.append(("cancel", o.placed_date + timedelta(days=1), o.card_amount_minor, "card"))
    elif o.verified is False and o.verify_date is not None:
        out.append(("verification_failed", o.verify_date, o.card_amount_minor, "card"))
    if "partial_refund" in o.breaks and o.delivery_date is not None:
        method = "store_credit" if o.store_credit_minor else "card"
        out.append(("partial", o.delivery_date + timedelta(days=1), int(o.breaks["partial_refund"]), method))  # type: ignore[call-overload]
    if "clawback" in o.breaks and o.delivery_date is not None:
        out.append(("clawback", o.delivery_date + timedelta(days=6), o.card_amount_minor, "card"))
    return out


def add_order(b: Builder, o: Order) -> None:
    status = "cancelled" if o.cancelled else ("failed_verification" if o.verified is False else "completed")
    if "clawback" in o.breaks:
        status = "refunded"
    b.add(
        "landing.orders",
        {
            "order_id": o.order_id,
            "entity_id": o.entity.entity_id,
            "program": o.program,
            "buyer_id": o.buyer_id,
            "seller_id": o.seller.seller_id,
            "sku": o.sku,
            "currency": o.currency,
            "item_price_minor": o.item_price_minor,
            "buyer_fee_minor": o.buyer_fee_minor,
            "shipping_minor": o.shipping_minor,
            "tax_minor": o.tax_minor,
            "incentive_minor": o.incentive_minor,
            "store_credit_minor": o.store_credit_minor,
            "card_amount_minor": o.card_amount_minor,
            "seller_fee_minor": o.seller_fee_minor,
            "seller_processing_fee_minor": o.seller_processing_fee_minor,
            "psp": o.psp,
            "psp_payment_ref": o.psp_ref,
            "placed_at": o.placed_at,
            "status": status,
        },
    )
    if o.verify_date is not None:
        _event(b, o, "verification_passed" if o.verified else "verification_failed", o.verify_date, 36_000)
    if o.ship_date is not None:
        _event(b, o, "shipped_to_buyer", o.ship_date, 50_000, tracking=o.tracking)
    if o.delivery_date is not None:
        _event(b, o, "delivered", o.delivery_date, 55_000, tracking=o.tracking)
    if o.return_close_date is not None:
        _event(b, o, "return_window_closed", o.return_close_date, 0)
    if o.cancelled:
        _event(b, o, "cancelled", o.placed_date + timedelta(days=1), 10_000)
    for kind, day, amount, method in refunds(o):
        _event(b, o, f"refund_{kind}", day, 40_000, amount=amount, method=method)
    if o.verified is False and o.verify_date is not None:
        _event(b, o, "seller_charge_assessed", o.verify_date, 37_000, amount=o.item_price_minor * 15 // 100)
    for name in ("split_tender", "partial_refund", "clawback"):
        if name in o.breaks:
            b.brk(name, "landing.orders", source_key([o.order_id]), str(o.breaks[name]))


def labels(o: Order) -> list[tuple[str, str, date]]:
    """(label id, leg, ship date) of every label an order needs."""
    if o.ship_date is None:
        return []
    if o.program == "verified_marketplace":
        inbound = o.placed_date + timedelta(days=1)
        return [
            (f"L{o.order_id}-1", "seller_to_authentication", inbound),
            (f"L{o.order_id}-2", "authentication_to_buyer", o.ship_date),
        ]
    return [(f"L{o.order_id}-1", "seller_to_buyer", o.ship_date)]


def add_labels(b: Builder, o: Order) -> None:
    s = Stream(b.seed, "labels", o.order_id)
    for label_id, leg, day in labels(o):
        weight = s.between(600, 3500)
        b.add(
            "landing.shipping_labels",
            {
                "label_id": label_id,
                "order_id": o.order_id,
                "entity_id": o.entity.entity_id,
                "carrier": CARRIER,
                "tracking_number": f"{o.tracking}{label_id[-1]}",
                "leg": leg,
                "service": "GROUND",
                "quoted_minor": 700 + weight // 2,
                "currency": o.entity.currency,
                "weight_grams": weight,
                "ship_date": day,
                "purchased_at": at(day, 30_000),
            },
        )


def add_tax(b: Builder, o: Order) -> None:
    status = "Voided" if o.cancelled else "Committed"
    b.add(
        "landing.tax_documents",
        {
            "document_code": o.order_id,
            "document_type": "SalesInvoice",
            "status": status,
            "entity_id": o.entity.entity_id,
            "document_date": o.placed_date,
            "currency": o.currency,
            "total_amount": decimal_text(o.item_price_minor),
            "total_tax": decimal_text(o.tax_minor),
            "jurisdiction": o.jurisdiction,
            "committed_at": None if o.cancelled else o.placed_at,
        },
    )
    if not o.cancelled:
        members = [
            ("a", "landing.orders", source_key([o.order_id])),
            ("b", "landing.tax_documents", source_key([o.order_id])),
        ]
        b.answer(
            10, f"T10-{o.order_id}", outcome="auto", entity=o.entity.entity_id, currency=o.currency, members=members
        )


def add_dispute(b: Builder, o: Order) -> None:
    outcome = o.breaks.get("chargeback")
    if outcome is None:
        return
    opened = (o.delivery_date or o.placed_date) + timedelta(days=12)
    b.add(
        "landing.disputes",
        {
            "dispute_id": f"dp_{o.order_id}",
            "psp": o.psp,
            "charge_ref": o.charge_ref,
            "order_id": o.order_id,
            "entity_id": o.entity.entity_id,
            "status": "won" if outcome == "late_win" else "lost",
            "reason": "fraudulent",
            "amount_minor": o.card_amount_minor,
            "fee_minor": 1500,
            "currency": o.currency,
            "opened_at": at(opened, 20_000),
            "closed_at": at(opened + timedelta(days=45), 20_000),
        },
    )
    b.brk("chargeback", "landing.disputes", source_key([f"dp_{o.order_id}"]), str(outcome))
    if outcome == "late_win":
        b.brk("late_win", "landing.disputes", source_key([f"dp_{o.order_id}"]))


def add_stored_value(b: Builder, o: Order) -> None:
    if o.store_credit_minor:
        b.add(
            "landing.stored_value_ledger",
            {
                "entry_id": f"SV{o.order_id}-r",
                "account_id": f"SV{o.buyer_id}",
                "entity_id": o.entity.entity_id,
                "type": "redeemed",
                "source": "checkout",
                "order_id": o.order_id,
                "amount_minor": -o.store_credit_minor,
                "currency": o.currency,
                "occurred_at": o.placed_at,
            },
        )
    for kind, day, amount, method in refunds(o):
        if method == "store_credit":
            b.add(
                "landing.stored_value_ledger",
                {
                    "entry_id": f"SV{o.order_id}-{kind}",
                    "account_id": f"SV{o.buyer_id}",
                    "entity_id": o.entity.entity_id,
                    "type": "issued",
                    "source": "refund",
                    "order_id": o.order_id,
                    "amount_minor": amount,
                    "currency": o.currency,
                    "occurred_at": at(day, 40_000),
                },
            )


def payables(o: Order) -> list[tuple[str, str, date, int]]:
    """(payable id, type, created, amount in the entity currency) for every seller payable of an order.

    Seller amounts are in the presentment currency of the order; the generator keeps seller payables and
    payouts in the entity's currency, converting cross-border orders at the reference rate of the day
    (``close_datagen.settlement`` passes the converted amounts in)."""
    out: list[tuple[str, str, date, int]] = []
    trigger = o.trigger_date
    if trigger is not None:
        out.append((f"P{o.order_id}-sale", "sale", trigger, o.seller_net_minor))
    if o.verified is False and o.verify_date is not None:
        out.append((f"P{o.order_id}-penalty", "penalty", o.verify_date, -(o.item_price_minor * 15 // 100)))
    if "clawback" in o.breaks and o.delivery_date is not None:
        out.append((f"P{o.order_id}-clawback", "clawback", o.delivery_date + timedelta(days=6), -o.seller_net_minor))
    return out
