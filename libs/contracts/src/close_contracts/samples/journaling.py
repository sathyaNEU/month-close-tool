"""SAMPLE journaling configuration, for testing only: not your company's accounting.

A coherent set of example answers and rules for the generated marketplace, written in the same shapes the
accounting team will use (``docs/domain/sample-journal-rules.md`` explains them in words):

- the policy answers;
- a chart of accounts and dimensions;
- account mappings;
- 13 event types;
- the automated journal definitions that journal them.

Every expression is ordinary rule DSL, so each of these can be edited, versioned and approved through the
API like any other configuration.
"""

from __future__ import annotations

from functools import reduce
from typing import Any, Final

from close_contracts.config.accounting_policy import AccountingPolicy
from close_contracts.config.journaling import (
    AccountMapping,
    AutoJournalDefinition,
    ChartOfAccounts,
    DimensionSet,
    EventType,
)
from close_contracts.default_sources import CARRIER_ACCOUNTS, col, fn, lit, lookup, s

J = dict[str, Any]


def n(value: int) -> J:
    return lit(value, "bigint")


def amt(role: str) -> J:
    return col(f"amount_{role}")


def attr(name: str) -> J:
    return col(f"attr_{name}")


def plus(*terms: J) -> J:
    return reduce(lambda a, b: fn("add", a, b), terms)


def minus(a: J, b: J) -> J:
    return fn("sub", a, b)


def when(condition: J, then: J, otherwise: J) -> J:
    return fn("if", condition, then, otherwise)


def eq(a: J, b: J) -> J:
    return fn("eq", a, b)


def is_(field: str, value: str) -> J:
    return eq(col(field), s(value))


def any_of(field: str, *values: str) -> J:
    return fn("or", *(is_(field, v) for v in values)) if len(values) > 1 else is_(field, values[0])


def smaller(a: J, b: J) -> J:
    return when(fn("le", a, b), a, b)


def decimal_minor(text: str, currency: str) -> J:
    return fn("to_minor_units", fn("parse_decimal", fn("coalesce", col(text), s("0"))), col(currency))


def account(code: str) -> J:
    return {"account": code}


def mapped(mapping_id: str, *keys: J) -> J:
    return {"mapping": mapping_id, "keys": list(keys)}


def line(line_id: str, side: str, acct: J, amount: J, condition: J | None = None, **dims: J) -> J:
    out: J = {"line_id": line_id, "side": side, "account": acct, "amount": amount}
    if condition is not None:
        out["condition"] = condition
    if dims:
        out["dimensions"] = dims
    return out


# ------------------------------------------------------------------------------------------ policy answers

SAMPLE_POLICY: Final = AccountingPolicy.model_validate(
    {
        "programs": [
            {
                "program": "verified_marketplace",
                "trigger": "verification_passed",
                "model": "agent",
                "shipping": "net_as_agent",
                "incentives": "contra_up_to_cumulative_then_marketing",
            },
            {
                "program": "direct_ship",
                "trigger": "shipped",
                "model": "agent",
                "shipping": "net_as_agent",
                "incentives": "contra_revenue",
            },
            {
                "program": "consignment",
                "trigger": "shipped",
                "model": "agent",
                "shipping": "net_as_agent",
                "incentives": "marketing",
            },
            {
                "program": "listings",
                "trigger": "delivered",
                "model": "agent",
                "shipping": "net_as_agent",
                "incentives": "contra_revenue",
            },
        ],
        "presentation": {
            "psp_receivable": "cash_equivalent",
            "processing_fees": "cost_of_revenue",
            "chargebacks": "reserve",
        },
    }
)

# ------------------------------------------------------------------------------------------ chart and dimensions


def _acct(code: str, name: str, kind: str, normal: str) -> J:
    return {"code": code, "name": name, "account_type": kind, "normal_balance": normal}


SAMPLE_CHART: Final = ChartOfAccounts.model_validate(
    {
        "chart_id": "sample_chart",
        "suspense_account": "9999",
        "fx_rounding_account": "7010",
        "accounts": [
            _acct("1010", "Cash - operating account", "asset", "debit"),
            _acct("1110", "PSP clearing (cash equivalent)", "asset", "debit"),
            _acct("1120", "Receivable from payment providers", "asset", "debit"),
            _acct("1130", "Payout provider clearing", "asset", "debit"),
            _acct("2010", "Seller payables", "liability", "credit"),
            _acct("2020", "Buyer deposits (order proceeds held)", "liability", "credit"),
            _acct("2110", "Sales tax payable", "liability", "credit"),
            _acct("2210", "Stored value liability", "liability", "credit"),
            _acct("2310", "Chargeback reserve", "liability", "credit"),
            _acct("2410", "Carrier accrual (labels not yet invoiced)", "liability", "credit"),
            _acct("2420", "Carrier payable", "liability", "credit"),
            _acct("2430", "Payment provider fees payable", "liability", "credit"),
            _acct("4010", "Commission revenue", "revenue", "credit"),
            _acct("4020", "Seller processing fee revenue", "revenue", "credit"),
            _acct("4030", "Buyer fee revenue", "revenue", "credit"),
            _acct("4040", "Shipping revenue", "revenue", "credit"),
            _acct("4050", "Product revenue (owned inventory)", "revenue", "credit"),
            _acct("4060", "Seller penalty revenue", "revenue", "credit"),
            _acct("4090", "Buyer incentives (contra revenue)", "revenue", "debit"),
            _acct("4095", "Refunds and concessions (contra revenue)", "revenue", "debit"),
            _acct("5010", "Cost of goods sold (owned inventory)", "expense", "debit"),
            _acct("5020", "Shipping expense", "expense", "debit"),
            _acct("5021", "Shipping cost recovered from buyers", "expense", "credit"),
            _acct("5030", "Payment processing fees (cost of revenue)", "expense", "debit"),
            _acct("6010", "Marketing - buyer incentives", "expense", "debit"),
            _acct("6030", "Payment processing fees (operating expense)", "expense", "debit"),
            _acct("6040", "Chargeback losses", "expense", "debit"),
            _acct("6050", "Dispute fees", "expense", "debit"),
            _acct("7010", "FX rounding", "expense", "debit"),
            _acct("9999", "Suspense", "asset", "debit"),
        ],
    }
)

PROGRAMS: Final = ("verified_marketplace", "direct_ship", "consignment", "listings")

SAMPLE_DIMENSIONS: Final = DimensionSet.model_validate(
    {
        "dimension_set_id": "sample_dimensions",
        "dimensions": [
            {
                "code": "program",
                "name": "Marketplace program",
                "required_on": ["revenue"],
                "values": [{"code": p, "name": p.replace("_", " ").title()} for p in PROGRAMS],
            },
            {
                "code": "psp",
                "name": "Payment provider",
                "required_accounts": ["1110", "1120"],
                "values": [{"code": "stripe", "name": "Stripe"}, {"code": "adyen", "name": "Adyen"}],
            },
        ],
    }
)

# ------------------------------------------------------------------------------------------ account mappings


def _mapping(mapping_id: str, description: str, key: str, rows: dict[str, str], default: str | None = None) -> J:
    out: J = {
        "mapping_id": mapping_id,
        "description": description,
        "keys": [key],
        "rows": [{"match": [k], "account": v} for k, v in rows.items()],
    }
    if default is not None:
        out["default_account"] = default
    return out


SAMPLE_MAPPINGS: Final = tuple(
    AccountMapping.model_validate(m)
    for m in (
        _mapping(
            "sample_psp_receivable_account",
            "Where money held by payment providers is carried",
            "presentation of the PSP receivable",
            {"cash_equivalent": "1110", "accounts_receivable": "1120"},
        ),
        _mapping(
            "sample_shipping_account",
            "Shipping charged to buyers",
            "shipping treatment",
            {"gross": "4040", "net_as_agent": "5021"},
        ),
        _mapping(
            "sample_processing_fee_account",
            "Payment processing fees",
            "presentation of processing fees",
            {"cost_of_revenue": "5030", "sga": "6030"},
        ),
        _mapping(
            "sample_chargeback_account",
            "Lost chargebacks",
            "chargeback treatment",
            {"reserve": "2310", "transaction_loss": "6040"},
        ),
        _mapping(
            "sample_carrier_charge_account",
            "Carrier invoice lines: transportation clears the label accrual, anything else is extra shipping cost",
            "charge type",
            {"transportation": "2410"},
            default="5020",
        ),
    )
)

# ------------------------------------------------------------------------------------------ event types

ORDER_AMOUNTS: Final = {
    "card": "card_amount_minor",
    "store_credit": "store_credit_minor",
    "item_price": "item_price_minor",
    "buyer_fee": "buyer_fee_minor",
    "shipping": "shipping_minor",
    "tax": "tax_minor",
    "incentive": "incentive_minor",
    "seller_fee": "seller_fee_minor",
    "processing_fee": "seller_processing_fee_minor",
}
PAYMENT_IDENTITY: Final = {
    "name": "payment",
    "expr": eq(
        plus(amt("card"), amt("store_credit")),
        minus(plus(amt("item_price"), amt("buyer_fee"), amt("shipping"), amt("tax")), amt("incentive")),
    ),
}
ORDER_LOOKUP: Final = {"alias": "order", "table": "landing.orders", "on": {"order_id": col("order_id")}}
STRIPE_ENTITY: Final = fn("regex_extract", col("account_id"), s("^acct_1Platform([A-Z0-9]+)"), n(1))
ADYEN_ENTITY: Final = fn("regex_extract", col("merchant_account"), s("^Platform([A-Z0-9]+)"), n(1))


def _order_fields(prefix: str = "") -> J:
    fields: J = {f"amount_{role}": col(prefix + column) for role, column in ORDER_AMOUNTS.items()}
    fields |= {
        "program": col(prefix + "program"),
        "seller_id": col(prefix + "seller_id"),
        "currency": col(prefix + "currency"),
        "attr_psp": col(prefix + "psp"),
    }
    return fields


def _event_type(  # noqa: PLR0917 (one row of a data table, read positionally)
    type_id: str,
    code: str,
    description: str,
    amounts: list[str],
    attrs: list[str],
    sources: list[J],
    identities: list[J] | None = None,
) -> J:
    return {
        "type_id": type_id,
        "event_type": code,
        "description": description,
        "amounts": amounts,
        "attributes": attrs,
        "identities": identities or [],
        "sources": sources,
    }


_ORDER_EVENTS = {"source_id": "order_events", "landing_table": "landing.order_events", "lookups": [ORDER_LOOKUP]}
_ORDER_EVENT_FIELDS = {
    "entity_id": col("entity_id"),
    "accounting_date": fn("date_of", col("occurred_at")),
    "order_id": col("order_id"),
}

SAMPLE_EVENT_TYPES: Final = tuple(
    EventType.model_validate(e)
    for e in (
        _event_type(
            "sample_order_captured",
            "ORDER_CAPTURED",
            "A buyer's order is captured: card and store credit taken, tax collected",
            list(ORDER_AMOUNTS),
            ["stage", "psp"],
            [
                {
                    "source_id": "orders",
                    "landing_table": "landing.orders",
                    "mapping": _order_fields()
                    | {
                        "entity_id": col("entity_id"),
                        "accounting_date": fn("date_of", col("placed_at")),
                        "order_id": col("order_id"),
                        "attr_stage": s("payment"),
                    },
                }
            ],
            [PAYMENT_IDENTITY],
        ),
        _event_type(
            "sample_order_stage",
            "ORDER_STAGE",
            "An order reaches a lifecycle stage: verification passed, shipped, delivered, return window closed",
            list(ORDER_AMOUNTS),
            ["stage", "psp"],
            [
                _ORDER_EVENTS
                | {
                    "filter": any_of(
                        "event_type", "verification_passed", "shipped_to_buyer", "delivered", "return_window_closed"
                    ),
                    "mapping": _order_fields("order_")
                    | _ORDER_EVENT_FIELDS
                    | {"attr_stage": when(is_("event_type", "shipped_to_buyer"), s("shipped"), col("event_type"))},
                }
            ],
            [PAYMENT_IDENTITY],
        ),
        _event_type(
            "sample_order_refund",
            "ORDER_REFUND",
            "A refund to the buyer: cancellation, failed verification, partial refund or clawback",
            ["refund", *ORDER_AMOUNTS],
            ["kind", "method", "psp"],
            [
                _ORDER_EVENTS
                | {
                    "filter": fn("starts_with", col("event_type"), s("refund_")),
                    "mapping": _order_fields("order_")
                    | _ORDER_EVENT_FIELDS
                    | {
                        "amount_refund": col("amount_minor"),
                        "attr_kind": fn("substring", col("event_type"), n(8), n(64)),
                        "attr_method": col("refund_method"),
                    },
                }
            ],
            [
                PAYMENT_IDENTITY,
                {
                    "name": "full_refund_before_trigger",
                    "expr": fn(
                        "or",
                        fn(
                            "and", fn("ne", attr("kind"), s("cancel")), fn("ne", attr("kind"), s("verification_failed"))
                        ),
                        eq(amt("refund"), amt("card")),
                    ),
                },
            ],
        ),
        _event_type(
            "sample_seller_charge",
            "SELLER_CHARGE",
            "A penalty charged to a seller after failed verification",
            ["penalty"],
            [],
            [
                _ORDER_EVENTS
                | {
                    "filter": is_("event_type", "seller_charge_assessed"),
                    "mapping": _ORDER_EVENT_FIELDS
                    | {
                        "amount_penalty": col("amount_minor"),
                        "currency": col("order_currency"),
                        "program": col("order_program"),
                        "seller_id": col("order_seller_id"),
                    },
                }
            ],
        ),
        _event_type(
            "sample_psp_fee",
            "PSP_FEE",
            "A payment provider's processing fee, taken out of the settlement",
            ["fee"],
            ["psp"],
            [
                {
                    "source_id": "stripe_charges",
                    "landing_table": "landing.stripe_balance_transactions",
                    "lookups": [{"alias": "order", "table": "landing.orders", "on": {"order_id": col("order_ref")}}],
                    "filter": fn("and", is_("type", "charge"), fn("gt", col("fee"), n(0))),
                    "mapping": {
                        "entity_id": STRIPE_ENTITY,
                        "currency": fn("upper", col("currency")),
                        "accounting_date": fn("date_of", col("created")),
                        "program": col("order_program"),
                        "order_id": col("order_ref"),
                        "amount_fee": col("fee"),
                        "attr_psp": s("stripe"),
                    },
                },
                {
                    "source_id": "adyen_settled",
                    "landing_table": "landing.adyen_settlement_details",
                    "lookups": [
                        {"alias": "order", "table": "landing.orders", "on": {"order_id": col("merchant_reference")}}
                    ],
                    "filter": is_("type", "Settled"),
                    "mapping": {
                        "entity_id": ADYEN_ENTITY,
                        "currency": col("net_currency"),
                        "accounting_date": fn("date_of", col("creation_date")),
                        "program": col("order_program"),
                        "order_id": col("merchant_reference"),
                        "amount_fee": decimal_minor("commission", "net_currency"),
                        "attr_psp": s("adyen"),
                    },
                },
            ],
        ),
        _event_type(
            "sample_psp_fee_billed",
            "PSP_FEE_BILLED",
            "A payment provider's monthly fee invoice (fees not taken out of the settlement)",
            ["fee"],
            ["psp"],
            [
                {
                    "source_id": "fee_invoices",
                    "landing_table": "landing.psp_fee_invoices",
                    "mapping": {
                        "entity_id": STRIPE_ENTITY,
                        "currency": col("currency"),
                        "accounting_date": col("invoice_date"),
                        "amount_fee": col("amount_minor"),
                        "attr_psp": col("psp"),
                    },
                }
            ],
        ),
        _event_type(
            "sample_psp_dispute",
            "PSP_DISPUTE",
            "A chargeback debited by the payment provider, or its reversal after a won dispute",
            ["amount", "fee"],
            ["direction", "psp"],
            [
                {
                    "source_id": "stripe_disputes",
                    "landing_table": "landing.stripe_balance_transactions",
                    "lookups": [{"alias": "order", "table": "landing.orders", "on": {"order_id": col("order_ref")}}],
                    "filter": fn("or", is_("type", "dispute"), is_("reporting_category", "dispute_reversal")),
                    "mapping": {
                        "entity_id": STRIPE_ENTITY,
                        "currency": fn("upper", col("currency")),
                        "accounting_date": fn("date_of", col("created")),
                        "program": col("order_program"),
                        "order_id": col("order_ref"),
                        "amount_amount": fn("abs", col("amount")),
                        "amount_fee": fn("abs", col("fee")),
                        "attr_direction": when(is_("type", "dispute"), s("lost"), s("reversed")),
                        "attr_psp": s("stripe"),
                    },
                },
                {
                    "source_id": "adyen_chargebacks",
                    "landing_table": "landing.adyen_settlement_details",
                    "lookups": [
                        {"alias": "order", "table": "landing.orders", "on": {"order_id": col("merchant_reference")}}
                    ],
                    "filter": any_of("type", "Chargeback", "ChargebackReversed"),
                    "mapping": {
                        "entity_id": ADYEN_ENTITY,
                        "currency": col("net_currency"),
                        "accounting_date": fn("date_of", col("creation_date")),
                        "program": col("order_program"),
                        "order_id": col("merchant_reference"),
                        "amount_amount": plus(
                            decimal_minor("net_debit", "net_currency"), decimal_minor("net_credit", "net_currency")
                        ),
                        "amount_fee": n(0),
                        "attr_direction": when(is_("type", "Chargeback"), s("lost"), s("reversed")),
                        "attr_psp": s("adyen"),
                    },
                },
            ],
        ),
        _event_type(
            "sample_psp_payout",
            "PSP_PAYOUT",
            "A payment provider pays out the settled balance to the platform's bank account",
            ["amount"],
            ["psp"],
            [
                {
                    "source_id": "stripe_payouts",
                    "landing_table": "landing.stripe_payouts",
                    "mapping": {
                        "entity_id": STRIPE_ENTITY,
                        "currency": fn("upper", col("currency")),
                        "accounting_date": fn("date_of", col("created")),
                        "amount_amount": col("amount"),
                        "attr_psp": s("stripe"),
                    },
                },
                {
                    "source_id": "adyen_payouts",
                    "landing_table": "landing.adyen_settlement_details",
                    "filter": is_("type", "MerchantPayout"),
                    "mapping": {
                        "entity_id": ADYEN_ENTITY,
                        "currency": col("net_currency"),
                        "accounting_date": fn("date_of", col("creation_date")),
                        "amount_amount": decimal_minor("net_debit", "net_currency"),
                        "attr_psp": s("adyen"),
                    },
                },
            ],
        ),
        _event_type(
            "sample_seller_payout",
            "SELLER_PAYOUT",
            "A payout to a seller completed through a payout provider",
            ["amount"],
            ["provider"],
            [
                {
                    "source_id": "payout_items",
                    "landing_table": "landing.payout_items",
                    "filter": is_("status", "COMPLETED"),
                    "mapping": {
                        "entity_id": col("entity_id"),
                        "currency": col("currency"),
                        "accounting_date": fn("date_of", col("created_at")),
                        "seller_id": col("seller_id"),
                        "amount_amount": decimal_minor("amount", "currency"),
                        "attr_provider": col("provider"),
                    },
                }
            ],
        ),
        _event_type(
            "sample_payout_funding",
            "PAYOUT_FUNDING",
            "The platform funds a payout provider from its bank account",
            ["amount"],
            ["provider"],
            [
                {
                    "source_id": "payout_funding",
                    "landing_table": "landing.payout_funding",
                    "mapping": {
                        "entity_id": col("entity_id"),
                        "currency": col("currency"),
                        "accounting_date": col("value_date"),
                        "amount_amount": decimal_minor("amount", "currency"),
                        "attr_provider": col("provider"),
                    },
                }
            ],
        ),
        _event_type(
            "sample_shipping_label",
            "SHIPPING_LABEL",
            "A shipping label bought at a quoted price",
            ["amount"],
            ["carrier"],
            [
                {
                    "source_id": "shipping_labels",
                    "landing_table": "landing.shipping_labels",
                    "mapping": {
                        "entity_id": col("entity_id"),
                        "currency": col("currency"),
                        "accounting_date": col("ship_date"),
                        "order_id": col("order_id"),
                        "amount_amount": col("quoted_minor"),
                        "attr_carrier": col("carrier"),
                    },
                }
            ],
        ),
        _event_type(
            "sample_carrier_charge",
            "CARRIER_CHARGE",
            "A carrier invoice line: the billed transportation, or a correction billed on top",
            ["amount"],
            ["charge_type", "carrier"],
            [
                {
                    "source_id": "carrier_invoice_lines",
                    "landing_table": "landing.carrier_invoice_lines",
                    "mapping": {
                        "entity_id": lookup("account_number", CARRIER_ACCOUNTS),
                        "currency": col("currency"),
                        "accounting_date": col("invoice_date"),
                        "amount_amount": col("billed_minor"),
                        "attr_charge_type": col("charge_type"),
                        "attr_carrier": col("carrier"),
                    },
                }
            ],
        ),
        _event_type(
            "sample_tax_remitted",
            "TAX_REMITTED",
            "Sales tax paid to a tax authority",
            ["amount"],
            ["jurisdiction"],
            [
                {
                    "source_id": "tax_remittances",
                    "landing_table": "landing.tax_remittances",
                    "mapping": {
                        "entity_id": col("entity_id"),
                        "currency": col("currency"),
                        "accounting_date": col("paid_date"),
                        "amount_amount": col("amount_minor"),
                        "attr_jurisdiction": col("jurisdiction"),
                    },
                }
            ],
        ),
    )
)

# -------------------------------------------------------------------------------- automated journal definitions

PSP_ACCOUNT: Final = mapped("sample_psp_receivable_account", col("policy_psp_receivable"))
SELLER_NET: Final = minus(minus(amt("item_price"), amt("seller_fee")), amt("processing_fee"))
DEPOSIT: Final = minus(plus(amt("card"), amt("store_credit")), amt("tax"))
AGENT: Final = is_("policy_model", "agent")
OWNED: Final = is_("policy_model", "owned_inventory")
# Revenue from the buyer on this order caps a buyer incentive booked as contra revenue: the buyer fee, plus
# shipping when it is booked gross, plus the item price when the platform owns the inventory.
BUYER_REVENUE: Final = plus(
    amt("buyer_fee"),
    when(is_("policy_shipping", "gross"), amt("shipping"), n(0)),
    when(is_("policy_model", "owned_inventory"), amt("item_price"), n(0)),
)
INCENTIVE_CONTRA: Final = when(
    is_("policy_incentives", "contra_revenue"),
    amt("incentive"),
    when(is_("policy_incentives", "marketing"), n(0), smaller(amt("incentive"), BUYER_REVENUE)),
)
INCENTIVE_MARKETING: Final = minus(amt("incentive"), INCENTIVE_CONTRA)
PROGRAM: Final = col("program")
PSP: Final = attr("psp")


def _flip(side: str, reverse: bool) -> str:
    return {"DR": "CR", "CR": "DR"}[side] if reverse else side


def _revenue_lines(prefix: str, *, reverse: bool) -> list[J]:
    """Revenue and the seller payable at the program's trigger (or their reversal)."""
    f = _flip
    return [
        line(f"{prefix}seller_payable", f("CR", reverse), account("2010"), SELLER_NET, AGENT),
        line(f"{prefix}commission", f("CR", reverse), account("4010"), amt("seller_fee"), AGENT, program=PROGRAM),
        line(
            f"{prefix}processing_fee", f("CR", reverse), account("4020"), amt("processing_fee"), AGENT, program=PROGRAM
        ),
        line(f"{prefix}product_revenue", f("CR", reverse), account("4050"), amt("item_price"), OWNED, program=PROGRAM),
        line(f"{prefix}cogs", f("DR", reverse), account("5010"), SELLER_NET, OWNED),
        line(f"{prefix}owned_seller_payable", f("CR", reverse), account("2010"), SELLER_NET, OWNED),
        line(f"{prefix}buyer_fee", f("CR", reverse), account("4030"), amt("buyer_fee"), program=PROGRAM),
        line(
            f"{prefix}shipping",
            f("CR", reverse),
            mapped("sample_shipping_account", col("policy_shipping")),
            amt("shipping"),
            program=PROGRAM,
        ),
        line(f"{prefix}incentive_contra", f("DR", reverse), account("4090"), INCENTIVE_CONTRA, program=PROGRAM),
        line(f"{prefix}incentive_marketing", f("DR", reverse), account("6010"), INCENTIVE_MARKETING, program=PROGRAM),
    ]


def _definition(definition_id: str, name: str, event_types: list[str], lines: list[J], condition: J | None = None) -> J:
    out: J = {"definition_id": definition_id, "name": name, "event_types": event_types, "lines": lines}
    if condition is not None:
        out["condition"] = condition
    return out


FULL_REFUND: Final = any_of("attr_kind", "cancel", "verification_failed")
AT_PAYMENT: Final = is_("policy_trigger", "payment")

SAMPLE_DEFINITIONS: Final = tuple(
    AutoJournalDefinition.model_validate(d)
    for d in (
        _definition(
            "sample_order_capture",
            "Capture: money held from the buyer, tax collected, store credit redeemed",
            ["ORDER_CAPTURED"],
            [
                line("psp", "DR", PSP_ACCOUNT, amt("card"), psp=PSP),
                line("store_credit", "DR", account("2210"), amt("store_credit")),
                line("tax", "CR", account("2110"), amt("tax")),
                line("deposits", "CR", account("2020"), DEPOSIT),
            ],
        ),
        _definition(
            "sample_revenue_at_trigger",
            "Revenue and the seller payable when the program's trigger is reached",
            ["ORDER_CAPTURED", "ORDER_STAGE"],
            [line("deposits", "DR", account("2020"), DEPOSIT), *_revenue_lines("", reverse=False)],
            condition=eq(attr("stage"), col("policy_trigger")),
        ),
        _definition(
            "sample_refund_before_trigger",
            "A full refund before the trigger: the capture is reversed",
            ["ORDER_REFUND"],
            [
                line("deposits", "DR", account("2020"), DEPOSIT),
                line("tax", "DR", account("2110"), amt("tax")),
                line("psp", "CR", PSP_ACCOUNT, amt("refund"), psp=PSP),
                line("store_credit", "CR", account("2210"), amt("store_credit")),
            ],
            condition=fn("and", FULL_REFUND, fn("not", AT_PAYMENT)),
        ),
        _definition(
            "sample_refund_after_trigger",
            "A full refund after revenue was booked at payment: the capture and the revenue are reversed",
            ["ORDER_REFUND"],
            [
                line("tax", "DR", account("2110"), amt("tax")),
                line("psp", "CR", PSP_ACCOUNT, amt("refund"), psp=PSP),
                line("store_credit", "CR", account("2210"), amt("store_credit")),
                *_revenue_lines("reverse_", reverse=True),
            ],
            condition=fn("and", FULL_REFUND, AT_PAYMENT),
        ),
        _definition(
            "sample_partial_refund",
            "A partial refund after delivery, borne by the platform as a concession",
            ["ORDER_REFUND"],
            [
                line("concession", "DR", account("4095"), amt("refund"), program=PROGRAM),
                line("psp", "CR", PSP_ACCOUNT, amt("refund"), is_("attr_method", "card"), psp=PSP),
                line("store_credit", "CR", account("2210"), amt("refund"), is_("attr_method", "store_credit")),
            ],
            condition=is_("attr_kind", "partial"),
        ),
        _definition(
            "sample_clawback",
            "A full refund after the seller was paid: the seller's net is clawed back, the rest is a concession",
            ["ORDER_REFUND"],
            [
                line("seller", "DR", account("2010"), SELLER_NET),
                line("concession", "DR", account("4095"), minus(amt("refund"), SELLER_NET), program=PROGRAM),
                line("psp", "CR", PSP_ACCOUNT, amt("refund"), psp=PSP),
            ],
            condition=is_("attr_kind", "clawback"),
        ),
        _definition(
            "sample_seller_penalty",
            "A penalty charged to the seller after failed verification",
            ["SELLER_CHARGE"],
            [
                line("seller", "DR", account("2010"), amt("penalty")),
                line("penalty", "CR", account("4060"), amt("penalty"), program=PROGRAM),
            ],
        ),
        _definition(
            "sample_psp_fee",
            "Processing fees taken out of the settlement",
            ["PSP_FEE"],
            [
                line("fee", "DR", mapped("sample_processing_fee_account", col("policy_processing_fees")), amt("fee")),
                line("psp", "CR", PSP_ACCOUNT, amt("fee"), psp=PSP),
            ],
        ),
        _definition(
            "sample_psp_fee_billed",
            "Processing fees invoiced monthly",
            ["PSP_FEE_BILLED"],
            [
                line("fee", "DR", mapped("sample_processing_fee_account", col("policy_processing_fees")), amt("fee")),
                line("payable", "CR", account("2430"), amt("fee")),
            ],
        ),
        _definition(
            "sample_psp_dispute",
            "A chargeback debited by the provider, or credited back after a won dispute",
            ["PSP_DISPUTE"],
            [
                line(
                    "lost_loss",
                    "DR",
                    mapped("sample_chargeback_account", col("policy_chargebacks")),
                    amt("amount"),
                    is_("attr_direction", "lost"),
                ),
                line("lost_fee", "DR", account("6050"), amt("fee"), is_("attr_direction", "lost")),
                line(
                    "lost_psp",
                    "CR",
                    PSP_ACCOUNT,
                    plus(amt("amount"), amt("fee")),
                    is_("attr_direction", "lost"),
                    psp=PSP,
                ),
                line(
                    "won_psp",
                    "DR",
                    PSP_ACCOUNT,
                    plus(amt("amount"), amt("fee")),
                    is_("attr_direction", "reversed"),
                    psp=PSP,
                ),
                line(
                    "won_loss",
                    "CR",
                    mapped("sample_chargeback_account", col("policy_chargebacks")),
                    amt("amount"),
                    is_("attr_direction", "reversed"),
                ),
                line("won_fee", "CR", account("6050"), amt("fee"), is_("attr_direction", "reversed")),
            ],
        ),
        _definition(
            "sample_psp_payout",
            "A provider payout reaches the bank",
            ["PSP_PAYOUT"],
            [
                line("cash", "DR", account("1010"), amt("amount")),
                line("psp", "CR", PSP_ACCOUNT, amt("amount"), psp=PSP),
            ],
        ),
        _definition(
            "sample_seller_payout",
            "A completed seller payout settles the seller payable",
            ["SELLER_PAYOUT"],
            [
                line("seller", "DR", account("2010"), amt("amount")),
                line("provider", "CR", account("1130"), amt("amount")),
            ],
        ),
        _definition(
            "sample_payout_funding",
            "The platform funds the payout provider",
            ["PAYOUT_FUNDING"],
            [
                line("provider", "DR", account("1130"), amt("amount")),
                line("cash", "CR", account("1010"), amt("amount")),
            ],
        ),
        _definition(
            "sample_shipping_label",
            "A label is bought: shipping expense and an accrual until the carrier invoices it",
            ["SHIPPING_LABEL"],
            [
                line("expense", "DR", account("5020"), amt("amount")),
                line("accrual", "CR", account("2410"), amt("amount")),
            ],
        ),
        _definition(
            "sample_carrier_charge",
            "A carrier invoice line: transportation clears the accrual, corrections are extra expense",
            ["CARRIER_CHARGE"],
            [
                line("charge", "DR", mapped("sample_carrier_charge_account", attr("charge_type")), amt("amount")),
                line("payable", "CR", account("2420"), amt("amount")),
            ],
        ),
        _definition(
            "sample_tax_remitted",
            "Sales tax paid to the authority",
            ["TAX_REMITTED"],
            [
                line("tax", "DR", account("2110"), amt("amount")),
                line("cash", "CR", account("1010"), amt("amount")),
            ],
        ),
    )
)
