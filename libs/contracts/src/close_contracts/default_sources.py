"""Default data sources for the expected landing shapes: identity keys and DSL mappings to canonical items.

Every landing table has an identity key (the columns that identify one source row, so a corrected row is a
new version of the same item and a re-delivered row under a new identity stays visible as a duplicate).
The tables that feed matching also have a default :class:`~close_contracts.config.data_source.DataSource`,
whose mapping fills the canonical item fields (``close_contracts.table_schemas.ITEM_FIELD_NAMES``) with
typed DSL expressions over the landing columns. Your real tables plug in by writing their own data source;
these defaults read the generator's shapes and double as worked examples, and a test typechecks every
mapping against the landing schema.
"""

from __future__ import annotations

from typing import Any, Final

from close_contracts.canonical_json import canonical_json
from close_contracts.config.data_source import DataSource

IDENTITY_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "landing.sellers": ("seller_id",),
    "landing.orders": ("order_id",),
    "landing.order_events": ("event_id",),
    "landing.shipping_labels": ("label_id",),
    "landing.carrier_invoice_lines": ("invoice_id", "line_id"),
    "landing.stripe_balance_transactions": ("id",),
    "landing.stripe_payouts": ("id",),
    "landing.adyen_settlement_details": (
        "merchant_account",
        "batch_number",
        "type",
        "psp_reference",
        "modification_reference",
    ),
    "landing.psp_fee_invoices": ("invoice_id",),
    "landing.disputes": ("dispute_id",),
    "landing.seller_payables": ("payable_id",),
    "landing.payout_items": ("provider", "payment_id"),
    "landing.payout_funding": ("provider", "funding_id"),
    "landing.bank_statement_lines": ("statement_id", "line_id"),
    "landing.tax_documents": ("document_code",),
    "landing.tax_remittances": ("remittance_id",),
    "landing.stored_value_ledger": ("entry_id",),
}


def source_key(values: list[Any]) -> str:
    """The canonical text of one row's identity key values (RFC 8785 JSON array)."""
    return canonical_json(values).decode("utf-8")


def col(name: str) -> dict[str, Any]:
    return {"col": name}


def lit(value: Any, type_: str) -> dict[str, Any]:
    return {"lit": value, "type": type_}


def fn(name: str, *args: dict[str, Any]) -> dict[str, Any]:
    return {"fn": name, "args": list(args)}


def s(value: str) -> dict[str, Any]:
    return lit(value, "string")


def _signed(amount: dict[str, Any]) -> dict[str, Any]:
    """Bank lines carry an unsigned amount and a C/D indicator."""
    return fn("if", fn("eq", col("credit_debit"), s("D")), fn("neg", amount), amount)


def _bank(fmt: str, parse: dict[str, Any]) -> DataSource:
    return DataSource.model_validate(
        {
            "source_id": f"bank_{fmt}",
            "description": f"Bank statement lines delivered as {fmt}",
            "landing_table": "landing.bank_statement_lines",
            "identity_key": list(IDENTITY_KEYS["landing.bank_statement_lines"]),
            "filter": fn("eq", col("format"), s(fmt)),
            "mapping": {
                "entity_id": fn("substring", col("account_id"), lit(1, "bigint"), lit(4, "bigint")),
                "currency": col("currency"),
                "amount_minor": _signed(fn("to_minor_units", parse, col("currency"))),
                "transaction_date": col("booking_date"),
                "value_date": col("value_date"),
                "reference": col("bank_reference"),
                "reference_2": col("end_to_end_id"),
                "reference_3": col("customer_reference"),
                "description": col("text"),
                "text_1": col("type_code"),
                "text_2": col("credit_debit"),
            },
            "incremental": {"mode": "column", "column": "booking_date", "lookback_days": 5},
        }
    )


def lookup(column: str, table: dict[str, str]) -> dict[str, Any]:
    """Map a column's value through a small fixed table (null when the value is not in it)."""
    out: dict[str, Any] = lit(None, "string")
    for key, value in sorted(table.items(), reverse=True):
        out = fn("if", fn("eq", col(column), s(key)), s(value), out)
    return out


CARRIER_ACCOUNTS: Final = {"FDX-US-100": "US01", "FDX-EU-200": "EU01", "FDX-UK-300": "UK01"}
"""The entity each carrier account bills. An invoice line from an unknown account gets no entity and is
rejected by normalization, so it shows in the control totals instead of disappearing."""


def _adyen_net() -> dict[str, Any]:
    net = fn(
        "sub",
        fn("to_minor_units", fn("parse_decimal", fn("coalesce", col("net_credit"), s("0"))), col("net_currency")),
        fn("to_minor_units", fn("parse_decimal", fn("coalesce", col("net_debit"), s("0"))), col("net_currency")),
    )
    return net


def _adyen_gross() -> dict[str, Any]:
    return fn(
        "sub",
        fn("to_minor_units", fn("parse_decimal", fn("coalesce", col("gross_credit"), s("0"))), col("gross_currency")),
        fn("to_minor_units", fn("parse_decimal", fn("coalesce", col("gross_debit"), s("0"))), col("gross_currency")),
    )


DEFAULT_SOURCES: Final = (
    DataSource.model_validate(
        {
            "source_id": "orders",
            "description": "Marketplace orders (presentment currency)",
            "landing_table": "landing.orders",
            "identity_key": ["order_id"],
            "mapping": {
                "entity_id": col("entity_id"),
                "currency": col("currency"),
                "amount_minor": col("card_amount_minor"),
                "transaction_date": fn("date_of", col("placed_at")),
                "reference": col("order_id"),
                "reference_2": col("psp_payment_ref"),
                "counterparty": col("buyer_id"),
                "description": col("program"),
                "text_1": col("psp"),
                "text_2": col("seller_id"),
                "text_3": col("status"),
                "number_1": col("tax_minor"),
                "number_2": col("store_credit_minor"),
                "number_3": col("item_price_minor"),
            },
        }
    ),
    DataSource.model_validate(
        {
            "source_id": "stripe_balance_transactions",
            "description": "Stripe balance transactions (settlement currency)",
            "landing_table": "landing.stripe_balance_transactions",
            "identity_key": ["id"],
            "mapping": {
                "entity_id": fn("regex_extract", col("account_id"), s("^acct_1Platform([A-Z0-9]+)"), lit(1, "bigint")),
                "currency": fn("upper", col("currency")),
                "amount_minor": col("amount"),
                "transaction_date": fn("date_of", col("created")),
                "value_date": col("available_on"),
                "reference": fn("coalesce", col("order_ref"), col("source_id")),
                "reference_2": col("payment_intent_id"),
                "reference_3": col("automatic_payout_id"),
                "description": fn("concat", col("type"), s(":"), col("reporting_category")),
                "text_1": col("type"),
                "text_2": fn("upper", col("customer_facing_currency")),
                "text_3": col("source_id"),
                "number_1": col("fee"),
                "number_2": col("net"),
                "number_3": col("customer_facing_amount"),
            },
        }
    ),
    DataSource.model_validate(
        {
            "source_id": "stripe_payouts",
            "description": "Stripe payouts",
            "landing_table": "landing.stripe_payouts",
            "identity_key": ["id"],
            "mapping": {
                "entity_id": fn("regex_extract", col("account_id"), s("^acct_1Platform([A-Z0-9]+)"), lit(1, "bigint")),
                "currency": fn("upper", col("currency")),
                "amount_minor": col("amount"),
                "transaction_date": fn("date_of", col("created")),
                "value_date": col("arrival_date"),
                "reference": col("id"),
                "reference_2": col("trace_id"),
                "text_1": col("status"),
            },
        }
    ),
    DataSource.model_validate(
        {
            "source_id": "adyen_settlement_details",
            "description": "Adyen settlement details (net in the settlement currency, gross in the payment currency)",
            "landing_table": "landing.adyen_settlement_details",
            "identity_key": list(IDENTITY_KEYS["landing.adyen_settlement_details"]),
            "mapping": {
                "entity_id": fn("regex_extract", col("merchant_account"), s("^Platform([A-Z0-9]+)"), lit(1, "bigint")),
                "currency": col("net_currency"),
                "amount_minor": _adyen_net(),
                "transaction_date": fn("date_of", col("creation_date")),
                "reference": col("merchant_reference"),
                "reference_2": col("psp_reference"),
                "reference_3": col("modification_reference"),
                "description": col("type"),
                "text_1": col("type"),
                "text_2": col("gross_currency"),
                "number_1": _adyen_gross(),
                "number_2": col("batch_number"),
            },
        }
    ),
    _bank("bai2", fn("parse_implied_decimals", col("amount_text"), lit(2, "bigint"))),
    _bank("camt053", fn("parse_decimal", col("amount_text"))),
    _bank("mt940", fn("parse_decimal_comma", col("amount_text"))),
    DataSource.model_validate(
        {
            "source_id": "seller_payables",
            "description": "The platform's seller-payable ledger",
            "landing_table": "landing.seller_payables",
            "identity_key": ["payable_id"],
            "mapping": {
                "entity_id": col("entity_id"),
                "currency": col("currency"),
                "amount_minor": col("amount_minor"),
                "transaction_date": fn("date_of", col("created_at")),
                "reference": col("transfer_group"),
                "reference_2": col("order_id"),
                "counterparty": col("seller_id"),
                "description": col("type"),
                "text_1": col("program"),
            },
        }
    ),
    DataSource.model_validate(
        {
            "source_id": "payout_items",
            "description": "Seller payout items from the payout providers",
            "landing_table": "landing.payout_items",
            "identity_key": ["provider", "payment_id"],
            "mapping": {
                "entity_id": col("entity_id"),
                "currency": col("currency"),
                "amount_minor": fn("to_minor_units", fn("parse_decimal", col("amount")), col("currency")),
                "transaction_date": fn("date_of", col("created_at")),
                "reference": col("client_payment_id"),
                "reference_2": col("payment_id"),
                "reference_3": col("batch_id"),
                "counterparty": col("seller_id"),
                "text_1": col("status"),
                "text_2": col("provider"),
            },
        }
    ),
    DataSource.model_validate(
        {
            "source_id": "shipping_labels",
            "description": "Shipping labels bought at a quoted price",
            "landing_table": "landing.shipping_labels",
            "identity_key": ["label_id"],
            "mapping": {
                "entity_id": col("entity_id"),
                "currency": col("currency"),
                "amount_minor": col("quoted_minor"),
                "transaction_date": col("ship_date"),
                "reference": col("tracking_number"),
                "reference_2": col("order_id"),
                "text_1": col("leg"),
                "number_1": col("weight_grams"),
            },
        }
    ),
    DataSource.model_validate(
        {
            "source_id": "carrier_invoice_lines",
            "description": "Carrier invoice lines, billed amounts",
            "landing_table": "landing.carrier_invoice_lines",
            "identity_key": ["invoice_id", "line_id"],
            "mapping": {
                "entity_id": lookup("account_number", CARRIER_ACCOUNTS),
                "currency": col("currency"),
                "amount_minor": col("billed_minor"),
                "transaction_date": col("ship_date"),
                "value_date": col("invoice_date"),
                "reference": col("tracking_number"),
                "reference_2": col("invoice_id"),
                "text_1": col("charge_type"),
                "number_1": col("billed_weight_grams"),
            },
        }
    ),
    DataSource.model_validate(
        {
            "source_id": "tax_documents",
            "description": "Committed tax documents from the tax engine",
            "landing_table": "landing.tax_documents",
            "identity_key": ["document_code"],
            "filter": fn("eq", col("status"), s("Committed")),
            "mapping": {
                "entity_id": col("entity_id"),
                "currency": col("currency"),
                "amount_minor": fn("to_minor_units", fn("parse_decimal", col("total_tax")), col("currency")),
                "transaction_date": col("document_date"),
                "reference": col("document_code"),
                "text_1": col("jurisdiction"),
                "text_2": col("status"),
            },
        }
    ),
    DataSource.model_validate(
        {
            "source_id": "tax_remittances",
            "description": "Sales tax remitted to the authorities",
            "landing_table": "landing.tax_remittances",
            "identity_key": ["remittance_id"],
            "mapping": {
                "entity_id": col("entity_id"),
                "currency": col("currency"),
                "amount_minor": col("amount_minor"),
                "transaction_date": col("paid_date"),
                "reference": col("remittance_id"),
                "text_1": col("jurisdiction"),
                "text_2": col("period"),
            },
        }
    ),
)
BY_ID: Final = {d.source_id: d for d in DEFAULT_SOURCES}
