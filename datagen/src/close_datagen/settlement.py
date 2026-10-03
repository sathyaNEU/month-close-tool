"""Provider, payout and bank flows that aggregate across orders, plus the answer key they determine.

Part one: payment-provider data. Stripe balance transactions settle into daily automatic payouts; Adyen
settlement lines settle into daily batches closed by a MerchantPayout line. Cross-border captures convert
at the reference rate of the day, shifted by the ``fx_difference`` break's basis points where it applies.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from close_contracts.default_sources import source_key
from close_datagen.landing import Builder, at, business_days_after, decimal_text, refunds
from close_datagen.plan import Order
from close_datagen.rng import Stream
from close_datagen.world import (
    ADYEN_COMPANY,
    ADYEN_MERCHANT,
    PSP_FEE_BPS,
    PSP_FEE_FIXED_MINOR,
    STRIPE_ACCOUNT,
    Entity,
)

Rates = dict[tuple[date, str, str], Decimal]
DISPUTE_FEE = 1500


@dataclass(frozen=True)
class Payout:
    """A provider payout that lands on the platform's bank account (template 3's left side)."""

    entity: Entity
    provider: str  # stripe or adyen
    table: str
    key: str
    amount_minor: int
    expected_value_date: date
    reference: str  # what the bank shows: the Stripe trace id or the Adyen TX...XT reference


def settle(amount: int, currency: str, entity: Entity, day: date, rates: Rates, *, bps: int = 0) -> int:
    if currency == entity.currency:
        return amount
    shift = Decimal(10_000 + bps) / Decimal(10_000)  # spark-ban: allow SB008 Python Decimal
    rate = rates[(day, currency, entity.currency)] * shift
    return int((Decimal(amount) * rate).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def captures(o: Order) -> list[tuple[str, int]]:
    """(capture suffix, presentment amount) of each capture of an order."""
    if "partial_capture" in o.breaks:
        first = int(o.breaks["partial_capture"])  # type: ignore[call-overload]
        return [("1", first), ("2", o.card_amount_minor - first)]
    delta = int(o.breaks.get("rounding", 0))  # type: ignore[call-overload]
    return [("1", o.card_amount_minor + delta)]


def _t1_outcome(o: Order) -> tuple[str, str | None]:
    if "rounding" in o.breaks:
        return "suggested", "rounding"
    for name in ("partial_capture", "split_tender", "fx_difference"):
        if name in o.breaks:
            return "auto", name
    return "auto", None


def _log_capture_breaks(b: Builder, o: Order, keys: list[tuple[str, str, str]]) -> None:
    for name in ("partial_capture", "rounding", "fx_difference"):
        if name in o.breaks:
            b.brk(name, keys[0][1], keys[0][2], str(o.breaks[name]))


def stripe_flows(b: Builder, orders: list[Order], rates: Rates) -> list[Payout]:
    by_account: dict[str, list[dict[str, object]]] = defaultdict(list)
    for o in (o for o in orders if o.psp == "stripe"):
        account = STRIPE_ACCOUNT[o.entity.entity_id]
        cur = o.entity.currency.lower()
        bps = int(o.breaks.get("fx_difference", 0))  # type: ignore[call-overload]
        charge_keys = []
        for i, (suffix, presentment) in enumerate(captures(o)):
            amount = settle(presentment, o.currency, o.entity, o.placed_date, rates, bps=bps)
            fee = (
                0
                if "fees_invoiced" in o.breaks
                else amount * PSP_FEE_BPS["stripe"] // 10_000 + PSP_FEE_FIXED_MINOR["stripe"]
            )
            row = {
                "id": f"txn_{o.order_id}_{suffix}",
                "account_id": account,
                "type": "charge",
                "reporting_category": "charge",
                "source_id": f"{o.charge_ref}_{suffix}" if suffix != "1" else o.charge_ref,
                "payment_intent_id": o.psp_ref,
                "order_ref": o.order_id,
                "amount": amount,
                "fee": fee,
                "net": amount - fee,
                "currency": cur,
                "created": o.placed_at + timedelta(seconds=i),
                "available_on": business_days_after(o.entity, o.placed_date, 2),
                "automatic_payout_id": None,
                "customer_facing_amount": presentment,
                "customer_facing_currency": o.currency.lower(),
            }
            by_account[account].append(row)
            charge_keys.append(("b", "landing.stripe_balance_transactions", source_key([row["id"]])))
            if "fees_invoiced" in o.breaks:
                b.brk("fees_invoiced", "landing.stripe_balance_transactions", source_key([row["id"]]))
        outcome, reason = _t1_outcome(o)
        _log_capture_breaks(b, o, charge_keys)
        b.answer(
            1,
            f"T1-{o.order_id}",
            outcome=outcome,
            entity=o.entity.entity_id,
            currency=o.currency,
            members=[("a", "landing.orders", source_key([o.order_id])), *charge_keys],
            break_type=reason,
        )
        for kind, day, amount, method in refunds(o):
            if method != "card":
                continue
            settled = -settle(amount, o.currency, o.entity, day, rates)
            by_account[account].append(
                {
                    "id": f"txn_{o.order_id}_re_{kind}",
                    "account_id": account,
                    "type": "refund",
                    "reporting_category": "refund",
                    "source_id": f"re_{o.order_id}_{kind}",
                    "payment_intent_id": o.psp_ref,
                    "order_ref": o.order_id,
                    "amount": settled,
                    "fee": 0,
                    "net": settled,
                    "currency": cur,
                    "created": at(day, 41_000),
                    "available_on": day,
                    "automatic_payout_id": None,
                    "customer_facing_amount": -amount,
                    "customer_facing_currency": o.currency.lower(),
                }
            )
        outcome_dispute = o.breaks.get("chargeback")
        if outcome_dispute is not None:
            opened = (o.delivery_date or o.placed_date) + timedelta(days=12)
            debit = settle(o.card_amount_minor, o.currency, o.entity, opened, rates)
            by_account[account].append(
                {
                    "id": f"txn_{o.order_id}_dp",
                    "account_id": account,
                    "type": "dispute",
                    "reporting_category": "dispute",
                    "source_id": f"dp_{o.order_id}",
                    "payment_intent_id": o.psp_ref,
                    "order_ref": o.order_id,
                    "amount": -debit,
                    "fee": DISPUTE_FEE,
                    "net": -debit - DISPUTE_FEE,
                    "currency": cur,
                    "created": at(opened, 20_000),
                    "available_on": opened,
                    "automatic_payout_id": None,
                    "customer_facing_amount": None,
                    "customer_facing_currency": None,
                }
            )
            if outcome_dispute == "late_win":
                won = opened + timedelta(days=75)
                by_account[account].append(
                    {
                        "id": f"txn_{o.order_id}_dpr",
                        "account_id": account,
                        "type": "adjustment",
                        "reporting_category": "dispute_reversal",
                        "source_id": f"dp_{o.order_id}",
                        "payment_intent_id": o.psp_ref,
                        "order_ref": o.order_id,
                        "amount": debit,
                        "fee": -DISPUTE_FEE,
                        "net": debit + DISPUTE_FEE,
                        "currency": cur,
                        "created": at(won, 20_000),
                        "available_on": won,
                        "automatic_payout_id": None,
                        "customer_facing_amount": None,
                        "customer_facing_currency": None,
                    }
                )
    return _stripe_payouts(b, by_account)


def _stripe_payouts(b: Builder, by_account: dict[str, list[dict[str, object]]]) -> list[Payout]:
    from close_datagen.world import BY_ID

    payouts: list[Payout] = []
    for account, rows in sorted(by_account.items()):
        entity = BY_ID[account.removeprefix("acct_1Platform")]
        by_day: dict[date, list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            by_day[row["available_on"]].append(row)  # type: ignore[index]
        for day in sorted(by_day):
            members = by_day[day]
            total = sum(int(r["net"]) for r in members)  # type: ignore[call-overload]
            if total <= 0:
                continue  # the balance carries over; those transactions stay without a payout id
            payout_id = f"po_{entity.entity_id}{day:%Y%m%d}"
            trace = f"TRC{Stream(b.seed, 'trace', payout_id).token(12)}"
            for row in members:
                row["automatic_payout_id"] = payout_id
            arrival = business_days_after(entity, day, 1)
            b.add(
                "landing.stripe_payouts",
                {
                    "id": payout_id,
                    "account_id": account,
                    "amount": total,
                    "currency": entity.currency.lower(),
                    "arrival_date": arrival,
                    "created": at(day, 7_200),
                    "status": "paid",
                    "trace_id": trace,
                    "destination": entity.bank_account,
                },
            )
            rows.append(
                {
                    "id": f"txn_{payout_id}",
                    "account_id": account,
                    "type": "payout",
                    "reporting_category": "payout",
                    "source_id": payout_id,
                    "payment_intent_id": None,
                    "order_ref": None,
                    "amount": -total,
                    "fee": 0,
                    "net": -total,
                    "currency": entity.currency.lower(),
                    "created": at(day, 7_200),
                    "available_on": day,
                    "automatic_payout_id": payout_id,
                    "customer_facing_amount": None,
                    "customer_facing_currency": None,
                }
            )
            key = source_key([payout_id])
            b.answer(
                2,
                f"T2-{payout_id}",
                outcome="auto",
                entity=entity.entity_id,
                currency=entity.currency,
                members=[
                    ("a", "landing.stripe_payouts", key),
                    *(("b", "landing.stripe_balance_transactions", source_key([r["id"]])) for r in members),
                ],
            )
            payouts.append(Payout(entity, "stripe", "landing.stripe_payouts", key, total, arrival, trace))
        for row in rows:
            b.add("landing.stripe_balance_transactions", dict(row))
    return payouts


def _adyen_line(
    merchant: str, batch: int, type_: str, created: date, seconds: int, **fields: object
) -> dict[str, object]:
    row: dict[str, object] = {
        "company_account": ADYEN_COMPANY,
        "merchant_account": merchant,
        "psp_reference": None,
        "merchant_reference": None,
        "payment_method": None,
        "creation_date": at(created, seconds),
        "type": type_,
        "modification_reference": None,
        "gross_currency": fields.get("net_currency"),
        "gross_debit": None,
        "gross_credit": None,
        "net_currency": fields.get("net_currency"),
        "net_debit": None,
        "net_credit": None,
        "commission": None,
        "markup": None,
        "scheme_fees": None,
        "interchange": None,
        "batch_number": batch,
    }
    row.update(fields)
    return row


def _adyen_key(row: dict[str, object]) -> str:
    return source_key(
        [row["merchant_account"], row["batch_number"], row["type"], row["psp_reference"], row["modification_reference"]]
    )


def adyen_flows(b: Builder, orders: list[Order], rates: Rates, start: date) -> list[Payout]:
    from close_datagen.world import BY_ID

    batches: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)

    def batch_of(entity: Entity, day: date) -> int:
        return (business_days_after(entity, day, 1) - start).days + 1

    for o in (o for o in orders if o.psp == "adyen"):
        merchant = ADYEN_MERCHANT[o.entity.entity_id]
        bps = int(o.breaks.get("fx_difference", 0))  # type: ignore[call-overload]
        batch = batch_of(o.entity, o.placed_date)
        keys = []
        for suffix, presentment in captures(o):
            settled = settle(presentment, o.currency, o.entity, o.placed_date, rates, bps=bps)
            fee = settled * PSP_FEE_BPS["adyen"] // 10_000 + PSP_FEE_FIXED_MINOR["adyen"]
            row = _adyen_line(
                merchant,
                batch,
                "Settled",
                o.placed_date,
                30_000,
                psp_reference=o.psp_ref,
                merchant_reference=o.order_id,
                payment_method="visa",
                modification_reference=f"CAP{o.psp_ref[-10:]}{suffix}",
                gross_currency=o.currency,
                gross_credit=decimal_text(presentment),
                net_currency=o.entity.currency,
                net_credit=decimal_text(settled - fee),
                commission=decimal_text(fee),
            )
            batches[(merchant, batch)].append(row)
            keys.append(("b", "landing.adyen_settlement_details", _adyen_key(row)))
        outcome, reason = _t1_outcome(o)
        _log_capture_breaks(b, o, keys)
        b.answer(
            1,
            f"T1-{o.order_id}",
            outcome=outcome,
            entity=o.entity.entity_id,
            currency=o.currency,
            members=[("a", "landing.orders", source_key([o.order_id])), *keys],
            break_type=reason,
        )
        for kind, day, amount, method in refunds(o):
            if method != "card":
                continue
            settled = settle(amount, o.currency, o.entity, day, rates)
            batches[(merchant, batch_of(o.entity, day))].append(
                _adyen_line(
                    merchant,
                    batch_of(o.entity, day),
                    "Refunded",
                    day,
                    41_000,
                    psp_reference=o.psp_ref,
                    merchant_reference=o.order_id,
                    modification_reference=f"REF{o.psp_ref[-10:]}{kind[:3]}",
                    gross_currency=o.currency,
                    gross_debit=decimal_text(amount),
                    net_currency=o.entity.currency,
                    net_debit=decimal_text(settled),
                )
            )
        outcome_dispute = o.breaks.get("chargeback")
        if outcome_dispute is not None:
            opened = (o.delivery_date or o.placed_date) + timedelta(days=12)
            debit = settle(o.card_amount_minor, o.currency, o.entity, opened, rates)
            batches[(merchant, batch_of(o.entity, opened))].append(
                _adyen_line(
                    merchant,
                    batch_of(o.entity, opened),
                    "Chargeback",
                    opened,
                    20_000,
                    psp_reference=o.psp_ref,
                    merchant_reference=o.order_id,
                    modification_reference=f"CB{o.psp_ref[-10:]}",
                    gross_currency=o.currency,
                    gross_debit=decimal_text(o.card_amount_minor),
                    net_currency=o.entity.currency,
                    net_debit=decimal_text(debit + DISPUTE_FEE),
                )
            )
            if outcome_dispute == "late_win":
                won = opened + timedelta(days=75)
                batches[(merchant, batch_of(o.entity, won))].append(
                    _adyen_line(
                        merchant,
                        batch_of(o.entity, won),
                        "ChargebackReversed",
                        won,
                        20_000,
                        psp_reference=o.psp_ref,
                        merchant_reference=o.order_id,
                        modification_reference=f"CBR{o.psp_ref[-10:]}",
                        gross_currency=o.currency,
                        gross_credit=decimal_text(o.card_amount_minor),
                        net_currency=o.entity.currency,
                        net_credit=decimal_text(debit),
                    )
                )

    payouts: list[Payout] = []
    for (merchant, batch), rows in sorted(batches.items()):
        entity = BY_ID[merchant.removeprefix("Platform")]

        def net(row: dict[str, object]) -> int:
            credit = int(Decimal(str(row["net_credit"])).scaleb(2)) if row["net_credit"] else 0
            debit = int(Decimal(str(row["net_debit"])).scaleb(2)) if row["net_debit"] else 0
            return credit - debit

        total = sum(net(r) for r in rows)
        for row in rows:
            b.add("landing.adyen_settlement_details", row)
        if total <= 0:
            continue
        day = start + timedelta(days=batch - 1)
        reference = f"TX{batch:05d}{entity.entity_id}XT"
        payout_row = _adyen_line(
            merchant,
            batch,
            "MerchantPayout",
            day,
            60_000,
            modification_reference=reference,
            net_currency=entity.currency,
            net_debit=decimal_text(total),
        )
        b.add("landing.adyen_settlement_details", payout_row)
        key = _adyen_key(payout_row)
        b.answer(
            2,
            f"T2-{merchant}-{batch}",
            outcome="auto",
            entity=entity.entity_id,
            currency=entity.currency,
            members=[
                ("a", "landing.adyen_settlement_details", key),
                *(("b", "landing.adyen_settlement_details", _adyen_key(r)) for r in rows),
            ],
        )
        payouts.append(
            Payout(
                entity,
                "adyen",
                "landing.adyen_settlement_details",
                key,
                total,
                business_days_after(entity, day, 1),
                reference,
            )
        )
    return payouts


@dataclass(frozen=True)
class Funding:
    entity: Entity
    provider: str
    key: str
    funding_id: str
    amount_minor: int
    value_date: date


def seller_flows(b: Builder, orders: list[Order], rates: Rates, rate_unclaimed: float) -> list[Funding]:
    from close_datagen.breaks import quota
    from close_datagen.landing import payables, transfer_group

    batches: dict[tuple[str, date], list[tuple[str, int]]] = defaultdict(list)
    doubled: set[tuple[str, date]] = set()  # batches that also hold a double-posted payable
    sellers_by_id = {o.seller.seller_id: o.seller for o in orders}
    entity_of = {o.seller.seller_id: o.entity for o in orders}
    for o in orders:
        for payable_id, type_, created, amount in payables(o):
            converted = settle(amount, o.currency, o.entity, created, rates)
            paid = business_days_after(o.entity, created, 1)
            group = transfer_group(o.seller, paid)
            row = {
                "payable_id": payable_id,
                "seller_id": o.seller.seller_id,
                "order_id": o.order_id,
                "entity_id": o.entity.entity_id,
                "program": o.program,
                "type": type_,
                "amount_minor": converted,
                "currency": o.entity.currency,
                "created_at": at(created, 45_000),
                "transfer_group": group,
            }
            b.add("landing.seller_payables", row)
            batches[(o.seller.seller_id, paid)].append((payable_id, converted))
            if type_ == "sale" and "duplicate" in o.breaks:
                doubled.add((o.seller.seller_id, paid))
                duplicate = dict(row, payable_id=f"{payable_id}-dup")
                b.add("landing.seller_payables", duplicate)
                b.brk("duplicate", "landing.seller_payables", source_key([duplicate["payable_id"]]))
                b.answer(
                    6,
                    f"T6-{duplicate['payable_id']}",
                    outcome="open",
                    entity=o.entity.entity_id,
                    currency=o.entity.currency,
                    members=[("b", "landing.seller_payables", source_key([duplicate["payable_id"]]))],
                    break_type="duplicate",
                )

    items = []
    for (seller_id, paid), members in sorted(batches.items()):
        seller, entity = sellers_by_id[seller_id], entity_of[seller_id]
        total = sum(amount for _, amount in members)
        payable_keys = [("b", "landing.seller_payables", source_key([pid])) for pid, _ in members]
        if total <= 0:
            b.answer(
                6,
                f"T6-{seller_id}-{paid:%Y%m%d}",
                outcome="open",
                entity=entity.entity_id,
                currency=entity.currency,
                members=payable_keys,
            )
            continue
        items.append((seller, entity, paid, total, payable_keys))
    unpaid = {
        (s.seller_id, p)
        for s, _, p, _, _ in quota(
            b.seed, "unclaimed_payout", rate_unclaimed, items, lambda i: f"{i[0].seller_id}|{i[2]}"
        )
    }

    fundings: dict[tuple[str, str, date], int] = defaultdict(int)
    for seller, entity, paid, total, payable_keys in items:
        provider = seller.payout_provider
        payment_id = f"{'pmt' if provider == 'hyperwallet' else 'PP'}-{seller.seller_id}-{paid:%Y%m%d}"
        status = "COMPLETED"
        if (seller.seller_id, paid) in unpaid:
            status = Stream(b.seed, "unclaimed", payment_id).pick(("RETURNED", "UNCLAIMED"))
        b.add(
            "landing.payout_items",
            {
                "provider": provider,
                "payment_id": payment_id,
                "client_payment_id": transfer_group(seller, paid),
                "batch_id": f"{provider[:2].upper()}B-{entity.entity_id}-{paid:%Y%m%d}",
                "seller_id": seller.seller_id,
                "entity_id": entity.entity_id,
                "amount": decimal_text(total),
                "currency": entity.currency,
                "status": status,
                "created_at": at(paid, 3_600),
                "updated_at": at(paid + timedelta(days=5 if status != "COMPLETED" else 0), 3_600),
            },
        )
        key = source_key([provider, payment_id])
        outcome, reason = ("open", "unclaimed_payout") if status != "COMPLETED" else ("auto", None)
        if outcome == "auto" and (seller.seller_id, paid) in doubled:
            # The original and its duplicate are identical, so the batch balances two ways: a reviewer must
            # choose, and the duplicate itself stays open (its own group below records the break).
            outcome = "suggested"
        if reason:
            b.brk(reason, "landing.payout_items", key, status)
        b.answer(
            6,
            f"T6-{payment_id}",
            outcome=outcome,
            entity=entity.entity_id,
            currency=entity.currency,
            members=[("a", "landing.payout_items", key), *payable_keys],
            break_type=reason,
        )
        fundings[(provider, entity.entity_id, paid)] += total

    from close_datagen.world import BY_ID

    out = []
    for (provider, entity_id, paid), amount in sorted(fundings.items()):
        entity = BY_ID[entity_id]
        funding_id = f"FND-{provider[:2].upper()}-{entity_id}-{paid:%Y%m%d}"
        b.add(
            "landing.payout_funding",
            {
                "provider": provider,
                "funding_id": funding_id,
                "entity_id": entity_id,
                "amount": decimal_text(amount),
                "currency": entity.currency,
                "funded_at": at(paid, 1_800),
                "value_date": paid,
                "bank_reference": funding_id,
            },
        )
        out.append(Funding(entity, provider, source_key([provider, funding_id]), funding_id, amount, paid))
    return out


def carrier_flows(b: Builder, orders: list[Order]) -> None:
    from close_datagen.world import BY_ID, CARRIER_ACCOUNT

    corrections = {o.order_id: o.breaks["carrier_correction"] for o in orders if "carrier_correction" in o.breaks}
    last_label = {}
    for row in b.rows["landing.shipping_labels"]:
        last_label[row["order_id"]] = row["label_id"]  # labels are added in leg order, so the last one wins
    invoices: dict[tuple[str, int, int], list[dict[str, object]]] = defaultdict(list)
    for row in b.rows["landing.shipping_labels"]:
        year, week, _ = row["ship_date"].isocalendar()
        invoices[(row["entity_id"], year, week)].append(row)
    for (entity_id, year, week), labels in sorted(invoices.items()):
        account = CARRIER_ACCOUNT[entity_id]
        invoice_id = f"INV-{account}-{year}{week:02d}"
        monday = date.fromisocalendar(year, week, 1) + timedelta(days=7)
        n = 0
        for label in sorted(labels, key=lambda r: str(r["label_id"])):
            members = [("a", "landing.shipping_labels", source_key([label["label_id"]]))]
            charges = [("transportation", int(str(label["quoted_minor"])))]
            correction = (
                corrections.get(str(label["order_id"]))
                if last_label.get(label["order_id"]) == label["label_id"]
                else None
            )
            if correction is not None:
                charges.append((str(correction[0]), int(correction[1])))  # type: ignore[index]
            for charge_type, amount in charges:
                n += 1
                line_id = f"{n:05d}"
                b.add(
                    "landing.carrier_invoice_lines",
                    {
                        "invoice_id": invoice_id,
                        "line_id": line_id,
                        "carrier": label["carrier"],
                        "account_number": account,
                        "tracking_number": label["tracking_number"],
                        "ship_date": label["ship_date"],
                        "service": label["service"],
                        "charge_type": charge_type,
                        "billed_weight_grams": label["weight_grams"],
                        "billed_minor": amount,
                        "currency": BY_ID[entity_id].currency,
                        "invoice_date": monday,
                    },
                )
                members.append(("b", "landing.carrier_invoice_lines", source_key([invoice_id, line_id])))
            currency = BY_ID[entity_id].currency
            if correction is None:
                b.answer(
                    9, f"T9-{label['label_id']}", outcome="auto", entity=entity_id, currency=currency, members=members
                )
                continue
            b.brk("carrier_correction", "landing.carrier_invoice_lines", members[-1][2], str(correction))
            if charges[1][1] == charges[0][1]:
                # The correction equals the quoted price, so either line alone balances the label: suggest it.
                b.answer(
                    9,
                    f"T9-{label['label_id']}",
                    outcome="suggested",
                    entity=entity_id,
                    currency=currency,
                    members=members,
                    break_type="carrier_correction",
                )
                continue
            # The label balances its transportation line; the correction is billed on top and needs a look.
            b.answer(
                9, f"T9-{label['label_id']}", outcome="auto", entity=entity_id, currency=currency, members=members[:2]
            )
            b.answer(
                9,
                f"T9-{label['label_id']}-correction",
                outcome="open",
                entity=entity_id,
                currency=currency,
                members=members[2:],
                break_type="carrier_correction",
            )


def tax_flows(b: Builder) -> None:
    groups: dict[tuple[str, str, str, str], list[dict[str, object]]] = defaultdict(list)
    for doc in b.rows["landing.tax_documents"]:
        if doc["status"] == "Committed":
            groups[
                (str(doc["entity_id"]), str(doc["jurisdiction"]), str(doc["currency"]), f"{doc['document_date']:%Y-%m}")
            ].append(doc)
    for (entity_id, jurisdiction, currency, period), docs in sorted(groups.items()):
        total = sum(int(Decimal(str(d["total_tax"])).scaleb(2)) for d in docs)
        year, month = int(period[:4]), int(period[5:])
        paid = date(year + month // 12, month % 12 + 1, 20)
        remittance_id = f"REM-{entity_id}-{jurisdiction}-{currency}-{period.replace('-', '')}"
        b.add(
            "landing.tax_remittances",
            {
                "remittance_id": remittance_id,
                "entity_id": entity_id,
                "jurisdiction": jurisdiction,
                "period": period,
                "amount_minor": total,
                "currency": currency,
                "paid_date": paid,
                "bank_reference": remittance_id,
            },
        )
        b.answer(
            10,
            f"T10-{remittance_id}",
            outcome="auto",
            entity=entity_id,
            currency=currency,
            members=[
                ("c", "landing.tax_remittances", source_key([remittance_id])),
                *(("b", "landing.tax_documents", source_key([d["document_code"]])) for d in docs),
            ],
        )


def fee_invoices(b: Builder) -> None:
    months: dict[tuple[str, str], int] = defaultdict(int)
    for row in b.rows["landing.stripe_balance_transactions"]:
        if row["type"] == "charge" and row["fee"] == 0:
            fee = int(row["amount"]) * PSP_FEE_BPS["stripe"] // 10_000 + PSP_FEE_FIXED_MINOR["stripe"]
            months[(str(row["account_id"]), f"{row['created']:%Y-%m}")] += fee
    for (account, period), amount in sorted(months.items()):
        year, month = int(period[:4]), int(period[5:])
        b.add(
            "landing.psp_fee_invoices",
            {
                "invoice_id": f"INV-STRIPE-{account[-4:]}-{period.replace('-', '')}",
                "psp": "stripe",
                "account_id": account,
                "period": period,
                "amount_minor": amount,
                "currency": "USD" if account.endswith("US01") else "GBP",
                "invoice_date": date(year + month // 12, month % 12 + 1, 3),
            },
        )


@dataclass(frozen=True)
class _Movement:
    entity: Entity
    credit: bool
    table: str
    key: str
    amount_minor: int
    value_date: date
    reference: str
    provider: str


_TYPE_CODES = {"bai2": ("165", "455"), "camt053": ("PMNT-RCDT-ESCT", "PMNT-ICDT-ESCT"), "mt940": ("NTRF", "NTRF")}


def _amount_text(fmt: str, minor: int) -> str:
    if fmt == "bai2":
        return str(minor)  # BAI2: no decimal point, two implied decimals
    text = decimal_text(minor)
    return text.replace(".", ",") if fmt == "mt940" else text


def _bank_text(fmt: str, m: _Movement) -> str:
    if m.provider == "stripe":
        return f"STRIPE TRANSFER TRN*1*{m.reference}" if fmt == "bai2" else f"STRIPE PAYOUT {m.reference}"
    if m.provider == "adyen":
        return f"ADYEN N.V. PAYOUT {m.reference}"
    return f"{m.provider.upper()} FUNDING {m.reference}"


def bank_flows(b: Builder, payouts: list[Payout], fundings: list[Funding], rate_timing: float) -> None:
    from close_datagen.breaks import quota

    moves = [
        _Movement(p.entity, True, p.table, p.key, p.amount_minor, p.expected_value_date, p.reference, p.provider)
        for p in payouts
    ]
    moves += [
        _Movement(
            f.entity, False, "landing.payout_funding", f.key, f.amount_minor, f.value_date, f.funding_id, f.provider
        )
        for f in fundings
    ]
    late = {m.key for m in quota(b.seed, "timing", rate_timing, moves, lambda m: m.key)}
    statements: dict[tuple[str, date], list[tuple[_Movement, date]]] = defaultdict(list)
    for m in moves:
        value = m.value_date
        if m.key in late:
            value = business_days_after(m.entity, value, Stream(b.seed, "timing", m.key).between(3, 5))
        statements[(m.entity.bank_account, value)].append((m, value))
    for (account, day), members in sorted(statements.items()):
        statement_id = f"STM-{account}-{day:%Y%m%d}"
        ordered = sorted(members, key=lambda mv: (not mv[0].credit, mv[0].reference))
        for n, (m, value) in enumerate(ordered, start=1):
            fmt = m.entity.bank_format
            line_id = f"{n:04d}"
            b.add(
                "landing.bank_statement_lines",
                {
                    "statement_id": statement_id,
                    "line_id": line_id,
                    "account_id": account,
                    "bank": m.entity.bank,
                    "format": fmt,
                    "booking_date": value,
                    "value_date": value,
                    "amount_text": _amount_text(fmt, m.amount_minor),
                    "credit_debit": "C" if m.credit else "D",
                    "currency": m.entity.currency,
                    "type_code": _TYPE_CODES[fmt][0 if m.credit else 1],
                    "bank_reference": m.reference
                    if fmt != "camt053"
                    else f"AS{Stream(b.seed, 'acctsvcr', m.key).token(10)}",
                    "customer_reference": None,
                    "end_to_end_id": m.reference if fmt == "camt053" else None,
                    "text": _bank_text(fmt, m),
                },
            )
            line_key = source_key([statement_id, line_id])
            if m.key in late:
                b.brk("timing", "landing.bank_statement_lines", line_key, f"{m.value_date} -> {value}")
            if m.credit:
                outcome, reason = ("suggested", "timing") if m.key in late else ("auto", None)
                b.answer(
                    3,
                    f"T3-{m.key}",
                    outcome=outcome,
                    entity=m.entity.entity_id,
                    currency=m.entity.currency,
                    members=[("a", m.table, m.key), ("b", "landing.bank_statement_lines", line_key)],
                    break_type=reason,
                )
