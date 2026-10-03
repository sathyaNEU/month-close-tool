"""The expected schema of every platform table (roadmap: contracts first, frozen at each gate).

The ``V###`` migrations in the engine (``close_engine/sql/ddl``) are the historical record of how tables
were created and changed; this registry is the schema they must add up to. A Spark test applies every
migration to an empty lake root and compares each table's actual schema (names, types, nullability,
partitioning, append-only) with the entry here, so the two can never drift apart silently.

Types are Spark SQL type names as ``DESCRIBE`` prints them. Money is BIGINT minor units (roadmap D9),
rates are DECIMAL(19,12), and DOUBLE never appears in a stored column (engineering rule 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from close_contracts.enums import Schema
from close_contracts.tables import LogicalTable

_TYPE: Final = re.compile(
    r"^(string|bigint|int|boolean|date|timestamp|decimal\(\d{1,2},\d{1,2}\)|map<string,(string|bigint)>|array<string>)$"
)
_COLUMN: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True
    comment: str = ""

    def __post_init__(self) -> None:
        if not _COLUMN.match(self.name):
            raise ValueError(f"invalid column name {self.name!r}")
        if not _TYPE.match(self.type):
            raise ValueError(f"{self.name}: unsupported type {self.type!r}")


@dataclass(frozen=True)
class TableSchema:
    table: LogicalTable
    columns: tuple[Column, ...]
    partitioned_by: tuple[str, ...] = ()
    append_only: bool = False
    comment: str = ""
    extra: dict[str, str] = field(default_factory=dict)  # further table properties
    natural_key: tuple[str, ...] = ()  # for append-only reference data: rows are inserted only if absent

    def __post_init__(self) -> None:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.table.key}: duplicate column names")
        missing = [p for p in self.partitioned_by if p not in names]
        if missing:
            raise ValueError(f"{self.table.key}: partition columns {missing} are not columns")
        unknown = [k for k in self.natural_key if k not in names]
        if unknown:
            raise ValueError(f"{self.table.key}: natural key columns {unknown} are not columns")

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)


def _c(name: str, type_: str, *, null: bool = False, comment: str = "") -> Column:
    """A column, NOT NULL unless ``null=True``."""
    return Column(name, type_, nullable=null, comment=comment)


def _n(name: str, type_: str, comment: str = "") -> Column:
    """A nullable column."""
    return Column(name, type_, nullable=True, comment=comment)


def _t(schema: Schema, name: str) -> LogicalTable:
    return LogicalTable(schema, name)


OPS: Final = Schema.OPS
REF: Final = Schema.REF
LANDING: Final = Schema.LANDING
MATCH: Final = Schema.MATCH
LEDGER: Final = Schema.LEDGER

# ---------------------------------------------------------------- ops: platform bookkeeping (Stage 1 tables keep
# the exact definitions the smoke job and the fence already create)

OPS_TABLES: Final = (
    TableSchema(
        _t(OPS, "schema_migrations"),
        (
            _c("version", "int"),
            _c("name", "string"),
            _c("checksum", "string", comment="sha256 of the migration file"),
            _c("statements", "int"),
            _c("applied_at", "timestamp"),
            _c("run_id", "string"),
            _c("engine_version", "string"),
        ),
        append_only=True,
        comment="Applied V### migrations; ddl_apply fails if an applied file has changed.",
    ),
    TableSchema(
        _t(OPS, "smoke_results"),
        (
            _n("run_id", "string"),
            _n("seq", "bigint"),
            _n("row_id", "string"),
            _n("entity_id", "string"),
            _n("currency", "string"),
            _n("amount_minor", "bigint"),
            _n("amount_eur_minor", "bigint"),
        ),
        partitioned_by=("run_id",),
        comment="Smoke job output (Stage 1), replaced per run_id.",
    ),
    TableSchema(
        _t(OPS, "run_manifests"),
        (
            _c("run_id", "string"),
            _c("attempt", "bigint"),
            _c("fencing_token", "bigint"),
            _c("job", "string"),
            _c("entity_id", "string"),
            _c("row_count", "bigint"),
            _c("digest", "string"),
        ),
        append_only=True,
    ),
    TableSchema(
        _t(OPS, "writer_claims"),
        (
            _c("resource", "string"),
            _c("fencing_token", "bigint"),
            _c("run_id", "string"),
            _c("attempt", "int"),
            _c("job", "string"),
            _c("claimed_at", "timestamp"),
        ),
        partitioned_by=("resource",),
        append_only=True,
    ),
    TableSchema(
        _t(OPS, "run_metrics"),
        (
            _c("run_id", "string"),
            _c("attempt", "int"),
            _c("fencing_token", "bigint"),
            _c("job", "string"),
            _c("metric", "string", comment="for example control_total.rows, control_total.amount, digest, rule.auto"),
            _c("dimension", "string", comment="what the metric is about: a table, a source and currency, a rule"),
            _n("value", "bigint"),
            _n("text", "string"),
            _c("recorded_at", "timestamp"),
        ),
        append_only=True,
        comment="Control totals, per-rule metrics, invariants and digests of every job run (long format).",
    ),
)

# ---------------------------------------------------------------- ref: reference data

REF_TABLES: Final = (
    TableSchema(
        _t(REF, "currencies"),
        (_c("currency", "string"), _c("exponent", "int"), _c("name", "string")),
    ),
    TableSchema(
        _t(REF, "entities"),
        (
            _c("entity_id", "string"),
            _c("name", "string"),
            _c("functional_currency", "string"),
            _c("country", "string"),
            _c("timezone", "string"),
            _c("calendar_code", "string"),
        ),
    ),
    TableSchema(
        _t(REF, "fx_rates"),
        (
            _c("rate_date", "date"),
            _c("from_currency", "string"),
            _c("to_currency", "string"),
            _c("rate_type", "string", comment="daily_spot or closing"),
            _c("rate", "decimal(19,12)"),
            _c("version", "int"),
            _c("source", "string"),
        ),
        append_only=True,
        natural_key=("rate_date", "from_currency", "to_currency", "rate_type", "version"),
    ),
    TableSchema(
        _t(REF, "calendars"),
        (
            _c("calendar_code", "string"),
            _c("day", "date"),
            _c("is_business_day", "boolean"),
            _c("bd_index", "int", comment="business-day index (close_contracts.calendar)"),
        ),
        partitioned_by=("calendar_code",),
    ),
    TableSchema(
        _t(REF, "periods"),
        (
            _c("entity_id", "string"),
            _c("period", "string", comment="YYYY-MM"),
            _c("start_date", "date"),
            _c("end_date", "date"),
            _c(
                "status",
                "string",
                comment="open or closed; Postgres is authoritative, this is the engine's pinned copy",
            ),
            _c("status_version", "int"),
        ),
    ),
    TableSchema(
        _t(REF, "account_mappings"),
        (
            _c("mapping_id", "string"),
            _c("version", "int"),
            _c("entity_id", "string"),
            _c("match_key", "string", comment="the attribute combination a posting rule looks up"),
            _c("account", "string"),
            _c("effective_from", "date"),
            _n("effective_to", "date"),
        ),
        append_only=True,
        natural_key=("mapping_id", "version"),
    ),
)

# ---------------------------------------------------------------- landing: the expected source shapes
# Each is the shape the generator writes and the default data sources read. Real tables plug in through their
# own data-source mapping, so these are a reference layout, not a requirement on your systems. Amounts keep the
# format of the real source: minor units where the source uses them (Stripe, our own systems), decimal strings
# where it does not (Adyen, payout providers, AvaTax), and the raw text of the file for bank statements.

LANDING_TABLES: Final = (
    TableSchema(
        _t(LANDING, "sellers"),
        (
            _c("seller_id", "string"),
            _c("entity_id", "string"),
            _c("country", "string"),
            _c("level", "int", comment="seller level, which sets the fee tier"),
            _c("fee_bps", "int"),
            _c("payout_provider", "string", comment="hyperwallet or paypal"),
            _c("created_at", "timestamp"),
        ),
    ),
    TableSchema(
        _t(LANDING, "orders"),
        (
            _c("order_id", "string"),
            _c("entity_id", "string"),
            _c("program", "string"),
            _c("buyer_id", "string"),
            _c("seller_id", "string"),
            _c("sku", "string"),
            _c("currency", "string", comment="presentment currency"),
            _c("item_price_minor", "bigint"),
            _c("buyer_fee_minor", "bigint"),
            _c("shipping_minor", "bigint"),
            _c("tax_minor", "bigint"),
            _c("incentive_minor", "bigint"),
            _c("store_credit_minor", "bigint", comment="split tender: paid with store credit"),
            _c("card_amount_minor", "bigint", comment="charged through the payment provider"),
            _c("seller_fee_minor", "bigint"),
            _c("seller_processing_fee_minor", "bigint"),
            _c("psp", "string", comment="stripe or adyen"),
            _n("psp_payment_ref", "string", comment="PaymentIntent id or Adyen merchant reference"),
            _c("placed_at", "timestamp"),
            _c("status", "string"),
        ),
    ),
    TableSchema(
        _t(LANDING, "order_events"),
        (
            _c("event_id", "string"),
            _c("order_id", "string"),
            _c("entity_id", "string"),
            _c("event_type", "string"),
            _c("occurred_at", "timestamp"),
            _n("tracking_number", "string"),
            _n("amount_minor", "bigint", comment="refunds and penalties"),
            _n("refund_method", "string", comment="card or store_credit"),
        ),
    ),
    TableSchema(
        _t(LANDING, "shipping_labels"),
        (
            _c("label_id", "string"),
            _c("order_id", "string"),
            _c("entity_id", "string"),
            _c("carrier", "string"),
            _c("tracking_number", "string"),
            _c(
                "leg",
                "string",
                comment="seller_to_authentication, authentication_to_buyer, seller_to_buyer, return_to_seller",
            ),
            _c("service", "string"),
            _c("quoted_minor", "bigint"),
            _c("currency", "string"),
            _c("weight_grams", "int"),
            _c("ship_date", "date"),
            _c("purchased_at", "timestamp"),
        ),
    ),
    TableSchema(
        _t(LANDING, "carrier_invoice_lines"),
        (
            _c("invoice_id", "string"),
            _c("line_id", "string"),
            _c("carrier", "string"),
            _c("account_number", "string"),
            _c("tracking_number", "string"),
            _c("ship_date", "date"),
            _c("service", "string"),
            _c(
                "charge_type", "string", comment="transportation, fuel_surcharge, address_correction, weight_adjustment"
            ),
            _c("billed_weight_grams", "int"),
            _c("billed_minor", "bigint"),
            _c("currency", "string"),
            _c("invoice_date", "date"),
        ),
    ),
    TableSchema(
        _t(LANDING, "stripe_balance_transactions"),
        (
            _c("id", "string", comment="txn_..."),
            _c("account_id", "string"),
            _c("type", "string", comment="charge, refund, adjustment, payout, stripe_fee, dispute"),
            _c("reporting_category", "string"),
            _n("source_id", "string", comment="ch_, re_, dp_ or po_ object"),
            _n("payment_intent_id", "string"),
            _n("order_ref", "string", comment="metadata[order_id]"),
            _c("amount", "bigint", comment="settlement currency, minor units, signed"),
            _c("fee", "bigint"),
            _c("net", "bigint"),
            _c("currency", "string", comment="lower case, as Stripe reports it"),
            _n("customer_facing_amount", "bigint", comment="what the buyer was charged, presentment currency"),
            _n("customer_facing_currency", "string"),
            _c("created", "timestamp"),
            _c("available_on", "date"),
            _n("automatic_payout_id", "string"),
        ),
    ),
    TableSchema(
        _t(LANDING, "stripe_payouts"),
        (
            _c("id", "string", comment="po_..."),
            _c("account_id", "string"),
            _c("amount", "bigint"),
            _c("currency", "string"),
            _c("arrival_date", "date"),
            _c("created", "timestamp"),
            _c("status", "string", comment="paid, failed, canceled"),
            _n("trace_id", "string"),
            _c("destination", "string", comment="the bank account"),
        ),
    ),
    TableSchema(
        _t(LANDING, "adyen_settlement_details"),
        (
            _c("company_account", "string"),
            _c("merchant_account", "string"),
            _n("psp_reference", "string"),
            _n("merchant_reference", "string"),
            _n("payment_method", "string"),
            _c("creation_date", "timestamp"),
            _c(
                "type",
                "string",
                comment="Settled, Refunded, Chargeback, ChargebackReversed, MerchantPayout, InvoiceDeduction, Fee",
            ),
            _n("modification_reference", "string"),
            _c("gross_currency", "string"),
            _n("gross_debit", "string", comment="decimal text, as in the report"),
            _n("gross_credit", "string"),
            _c("net_currency", "string"),
            _n("net_debit", "string"),
            _n("net_credit", "string"),
            _n("commission", "string"),
            _n("markup", "string"),
            _n("scheme_fees", "string"),
            _n("interchange", "string"),
            _c("batch_number", "int"),
        ),
    ),
    TableSchema(
        _t(LANDING, "psp_fee_invoices"),
        (
            _c("invoice_id", "string"),
            _c("psp", "string"),
            _c("account_id", "string"),
            _c("period", "string"),
            _c("amount_minor", "bigint"),
            _c("currency", "string"),
            _c("invoice_date", "date"),
        ),
    ),
    TableSchema(
        _t(LANDING, "disputes"),
        (
            _c("dispute_id", "string"),
            _c("psp", "string"),
            _c("charge_ref", "string"),
            _c("order_id", "string"),
            _c("entity_id", "string"),
            _c("status", "string", comment="needs_response, under_review, won, lost"),
            _c("reason", "string"),
            _c("amount_minor", "bigint"),
            _c("fee_minor", "bigint"),
            _c("currency", "string"),
            _c("opened_at", "timestamp"),
            _n("closed_at", "timestamp"),
        ),
    ),
    TableSchema(
        _t(LANDING, "seller_payables"),
        (
            _c("payable_id", "string"),
            _c("seller_id", "string"),
            _n("order_id", "string"),
            _c("entity_id", "string"),
            _c("program", "string"),
            _c("type", "string", comment="sale, penalty, clawback, adjustment"),
            _c("amount_minor", "bigint", comment="net proceeds; negative for penalties and clawbacks"),
            _c("currency", "string"),
            _c("created_at", "timestamp"),
            _c("transfer_group", "string", comment="the payout reference sent to the payout provider"),
        ),
    ),
    TableSchema(
        _t(LANDING, "payout_items"),
        (
            _c("provider", "string", comment="hyperwallet or paypal"),
            _c("payment_id", "string"),
            _c(
                "client_payment_id",
                "string",
                comment="our transfer_group (Hyperwallet clientPaymentId, PayPal sender_item_id)",
            ),
            _c("batch_id", "string"),
            _c("seller_id", "string"),
            _c("entity_id", "string"),
            _c("amount", "string", comment="decimal text, as the providers report it"),
            _c("currency", "string"),
            _c("status", "string", comment="COMPLETED, FAILED, RETURNED, UNCLAIMED"),
            _c("created_at", "timestamp"),
            _c("updated_at", "timestamp"),
        ),
    ),
    TableSchema(
        _t(LANDING, "payout_funding"),
        (
            _c("provider", "string"),
            _c("funding_id", "string"),
            _c("entity_id", "string"),
            _c("amount", "string"),
            _c("currency", "string"),
            _c("funded_at", "timestamp"),
            _c("value_date", "date"),
            _c("bank_reference", "string"),
        ),
    ),
    TableSchema(
        _t(LANDING, "bank_statement_lines"),
        (
            _c("statement_id", "string"),
            _c("line_id", "string"),
            _c("account_id", "string"),
            _c("bank", "string"),
            _c("format", "string", comment="bai2, camt053 or mt940"),
            _c("booking_date", "date"),
            _c("value_date", "date"),
            _c("amount_text", "string", comment="raw: BAI2 implied decimals, MT940 decimal comma, camt.053 decimal"),
            _c("credit_debit", "string", comment="C or D"),
            _c("currency", "string"),
            _n("type_code", "string", comment="BAI2 type code or camt.053 bank transaction code"),
            _n("bank_reference", "string", comment="BAI2 bank reference, camt AcctSvcrRef, MT940 :61: bank reference"),
            _n("customer_reference", "string"),
            _n("end_to_end_id", "string"),
            _n("text", "string", comment="BAI2 88 records, camt RmtInf, MT940 :86:"),
        ),
    ),
    TableSchema(
        _t(LANDING, "tax_documents"),
        (
            _c("document_code", "string", comment="the order id"),
            _c("document_type", "string", comment="SalesInvoice or ReturnInvoice"),
            _c("status", "string", comment="Committed, Uncommitted or Voided"),
            _c("entity_id", "string"),
            _c("document_date", "date"),
            _c("currency", "string"),
            _c("total_amount", "string", comment="decimal text, as AvaTax reports it"),
            _c("total_tax", "string"),
            _c("jurisdiction", "string"),
            _n("committed_at", "timestamp"),
        ),
    ),
    TableSchema(
        _t(LANDING, "tax_remittances"),
        (
            _c("remittance_id", "string"),
            _c("entity_id", "string"),
            _c("jurisdiction", "string"),
            _c("period", "string"),
            _c("amount_minor", "bigint"),
            _c("currency", "string"),
            _c("paid_date", "date"),
            _c("bank_reference", "string"),
        ),
    ),
    TableSchema(
        _t(LANDING, "stored_value_ledger"),
        (
            _c("entry_id", "string"),
            _c("account_id", "string", comment="the buyer's stored-value account"),
            _c("entity_id", "string"),
            _c("type", "string", comment="issued, redeemed, expired, breakage, escheat"),
            _c("source", "string", comment="refund, gift_card_sale, promotion, checkout"),
            _n("order_id", "string"),
            _c("amount_minor", "bigint", comment="positive when issued, negative when used or removed"),
            _c("currency", "string"),
            _c("occurred_at", "timestamp"),
        ),
    ),
)

# ---------------------------------------------------------------- match: the matching engine's outputs (Stage 3)
# Item content is per data source and versioned (append-only); item and group state is per match set.

_ITEM_FIELDS: Final = (
    _c("entity_id", "string"),
    _c("currency", "string"),
    _c("amount_minor", "bigint"),
    _c("transaction_date", "date"),
    _n("value_date", "date"),
    _n("reference", "string"),
    _n("reference_2", "string"),
    _n("reference_3", "string"),
    _n("description", "string"),
    _n("counterparty", "string"),
    *(_n(f"text_{i}", "string") for i in range(1, 7)),
    *(_n(f"number_{i}", "bigint") for i in range(1, 5)),
    *(_n(f"date_{i}", "date") for i in range(1, 3)),
)
ITEM_FIELD_NAMES: Final = tuple(c.name for c in _ITEM_FIELDS)
"""The canonical item fields a data-source mapping can fill (core fields plus generic typed slots)."""

MATCH_TABLES: Final = (
    TableSchema(
        _t(MATCH, "item_versions"),
        (
            _c("data_source_id", "string"),
            _c("item_id", "string"),
            _c("item_version", "int"),
            *_ITEM_FIELDS,
            _c("source_table", "string"),
            _c("source_version", "bigint", comment="the landing table's Delta version read"),
            _c("source_key", "string", comment="canonical JSON of the identity key values"),
            _c("row_hash", "string", comment="sha256 of the landing row"),
            _c("is_void", "boolean", comment="the source row was deleted"),
            _c("run_id", "string"),
            _c("created_at", "timestamp"),
        ),
        partitioned_by=("data_source_id",),
        append_only=True,
    ),
    TableSchema(
        _t(MATCH, "items"),
        (
            _c("match_set_id", "string"),
            _c("item_id", "string"),
            _c("data_source_id", "string"),
            _c("role", "string"),
            _c("item_version", "int"),
            _c("status", "string", comment="open, matched, suggested, in_transit, voided"),
            _n("group_id", "string"),
            _n("suggestion_group_id", "string"),
            _c("updated_run_id", "string"),
            _c("updated_at", "timestamp"),
        ),
        partitioned_by=("match_set_id",),
        comment="Current state per item and match set (MERGE on the literal match_set_id partition).",
    ),
    TableSchema(
        _t(MATCH, "groups"),
        (
            _c("match_set_id", "string"),
            _c("group_id", "string"),
            _n("rule_id", "string"),
            _c("match_type", "string", comment="automatic, suggested, manual, partial"),
            _c("status", "string", comment="active, pending, confirmed, rejected, superseded, unmatched, broken"),
            _c("ambiguous", "boolean"),
            _c("currency", "string"),
            _c("difference_minor", "bigint"),
            _n("reason_code", "string"),
            _c("created_run_id", "string"),
            _c("created_at", "timestamp"),
            _c("updated_run_id", "string"),
            _c("updated_at", "timestamp"),
        ),
        partitioned_by=("match_set_id",),
    ),
    TableSchema(
        _t(MATCH, "group_items"),
        (
            _c("match_set_id", "string"),
            _c("group_id", "string"),
            _c("item_id", "string"),
            _c("item_version", "int"),
            _c("role", "string"),
            _c("amount_minor", "bigint"),
        ),
        partitioned_by=("match_set_id",),
        append_only=True,
    ),
    TableSchema(
        _t(MATCH, "adjustment_requests"),
        (
            _c("match_set_id", "string"),
            _c("request_id", "string"),
            _c("action", "string", comment="create or cancel"),
            _c("group_id", "string"),
            _c("reason_code", "string"),
            _c("entity_id", "string"),
            _c("currency", "string"),
            _c("amount_minor", "bigint"),
            _c("run_id", "string"),
            _c("created_at", "timestamp"),
        ),
        partitioned_by=("match_set_id",),
        append_only=True,
    ),
    TableSchema(
        _t(MATCH, "item_state_log"),
        (
            _c("match_set_id", "string"),
            _c("item_id", "string"),
            _c("run_id", "string"),
            _c("seq", "int"),
            _n("from_status", "string"),
            _c("to_status", "string"),
            _n("group_id", "string"),
            _c("reason", "string"),
            _c("at", "timestamp"),
        ),
        partitioned_by=("match_set_id",),
        append_only=True,
    ),
    TableSchema(
        _t(MATCH, "timing_classifications"),
        (
            _c("match_set_id", "string"),
            _c("item_id", "string"),
            _c("rule_id", "string"),
            _c("expected_clear_date", "date"),
            _c("run_id", "string"),
            _c("classified_at", "timestamp"),
        ),
        partitioned_by=("match_set_id",),
        append_only=True,
    ),
)

# ---------------------------------------------------------------- ledger: the subledger and GL outputs (Stage 6)

LEDGER_TABLES: Final = (
    TableSchema(
        _t(LEDGER, "accounting_events"),
        (
            _c("event_id", "string"),
            _c("event_version", "int"),
            _c("event_type", "string"),
            _c("entity_id", "string"),
            _n("program", "string"),
            _n("order_id", "string"),
            _n("seller_id", "string"),
            _c("occurred_at", "timestamp"),
            _c("accounting_date", "date"),
            _c("currency", "string"),
            _c("amounts_minor", "map<string,bigint>", comment="amount by role, for example gross, fee, tax"),
            _c("attributes", "map<string,string>"),
            _c("is_reversal", "boolean"),
            _c("source_table", "string"),
            _c("source_key", "string"),
            _c("source_version", "bigint"),
            _c("run_id", "string"),
            _c("created_at", "timestamp"),
        ),
        partitioned_by=("entity_id",),
        append_only=True,
    ),
    TableSchema(
        _t(LEDGER, "subledger_lines"),
        (
            _c("line_id", "string"),
            _c("event_id", "string"),
            _c("event_version", "int"),
            _c("posting_rule_id", "string"),
            _c("posting_rule_version", "int"),
            _c("entity_id", "string"),
            _c("accounting_date", "date"),
            _c("period", "string"),
            _c("account", "string"),
            _c("side", "string", comment="DR or CR"),
            _c("amount_minor", "bigint"),
            _c("currency", "string"),
            _c("functional_amount_minor", "bigint"),
            _c("functional_currency", "string"),
            _n("fx_rate", "decimal(19,12)"),
            _n("fx_rate_type", "string"),
            _n("fx_rate_date", "date"),
            _n("fx_rate_version", "int"),
            _c("dimensions", "map<string,string>"),
            _c("description", "string"),
            _c("rollup_line_id", "string"),
            _c("is_suspense", "boolean"),
            _n("intended_account", "string"),
            _c("is_prior_period_adjustment", "boolean"),
            _n("original_period", "string"),
            _c("run_id", "string"),
            _c("created_at", "timestamp"),
        ),
        partitioned_by=("entity_id",),
        append_only=True,
        natural_key=("line_id",),  # Stage 8a: written insert-only, so a rerun writes nothing twice
    ),
    TableSchema(
        _t(LEDGER, "gl_rollup_lines"),
        (
            _c("rollup_line_id", "string"),
            _c("entity_id", "string"),
            _c("period", "string"),
            _n("accounting_date", "date", comment="set for daily rollups"),
            _c("account", "string"),
            _c("dimensions_key", "string"),
            _c("dimensions", "map<string,string>"),
            _c("currency", "string"),
            _c("debit_minor", "bigint"),
            _c("credit_minor", "bigint"),
            _c("line_count", "bigint"),
            _c("run_id", "string"),
            _c("created_at", "timestamp"),
            # Stage 8a (V009 adds these; the journaling stand-in always fills them):
            _n("functional_debit_minor", "bigint", "debits in the entity's functional currency"),
            _n("functional_credit_minor", "bigint", "credits in the entity's functional currency"),
        ),
        partitioned_by=("entity_id",),
        append_only=True,
        natural_key=("rollup_line_id",),
    ),
    TableSchema(
        _t(LEDGER, "gl_outbox"),
        (
            _c("outbox_id", "string"),
            _c("rollup_line_id", "string"),
            _c("entity_id", "string"),
            _c("period", "string"),
            _c("payload", "string", comment="canonical JSON sent to the general ledger"),
            _c("run_id", "string"),
            _c("created_at", "timestamp"),
        ),
        append_only=True,
    ),
    TableSchema(
        _t(LEDGER, "adjustment_consumption"),
        (
            _c("request_id", "string"),
            _c("match_set_id", "string"),
            _c("event_id", "string"),
            _c("run_id", "string"),
            _c("consumed_at", "timestamp"),
        ),
        append_only=True,
        comment="Each adjustment request is booked exactly once.",
    ),
    TableSchema(
        _t(LEDGER, "journal_lines"),
        (
            _c("journal_id", "string"),
            _c("entity_id", "string"),
            _c("number", "bigint", comment="gapless journal number within the entity"),
            _c("revision", "int"),
            _c("content_hash", "string", comment="the content the approvals were bound to"),
            _c("line_no", "bigint"),
            _c("template_id", "string"),
            _c("source", "string", comment="manual, import, recurring or reversal"),
            _c("period", "string"),
            _c("accounting_date", "date"),
            _c("account", "string"),
            _c("side", "string", comment="DR or CR"),
            _c("amount_minor", "bigint"),
            _c("currency", "string"),
            _c("functional_amount_minor", "bigint"),
            _c("functional_currency", "string"),
            _c("debit_minor", "bigint", comment="functional amount on the debit side, else 0"),
            _c("credit_minor", "bigint", comment="functional amount on the credit side, else 0"),
            _c("dimensions", "map<string,string>"),
            _c("line_description", "string"),
            _c("journal_description", "string"),
            _c("preparer", "string"),
            _c(
                "approvals",
                "string",
                comment="level:approver per level, plus for <delegator> when delegated, separated by semicolons",
            ),
            _c("approved_at", "timestamp"),
            _c("run_id", "string", comment="the posting run"),
            _c("posted_at", "timestamp", comment="when the posting run was requested (Postgres time), on every line"),
            # Stage 7c (V008 adds these; rows posted before it keep nulls):
            _n("fx_rate", "decimal(19,12)", "the rate a foreign-currency line was converted at (D9)"),
            _n("fx_rate_date", "date"),
            _n("fx_rate_type", "string"),
            _n("fx_rate_version", "int"),
            _n("line_kind", "string", "entry, or fx_rounding for the rounding line of a foreign-currency journal"),
        ),
        partitioned_by=("entity_id",),
        append_only=True,
        natural_key=("journal_id", "content_hash", "line_no"),
        comment="Posted manual journal lines (Stage 7b): the authoritative ledger, written in one commit per run.",
    ),
    TableSchema(
        _t(LEDGER, "journal_postings"),
        (
            _c("journal_id", "string"),
            _c("entity_id", "string"),
            _c("number", "bigint"),
            _c("period", "string"),
            _c("content_hash", "string"),
            _c("payload", "string", comment="canonical JSON of the journal and its lines, for the ERP"),
            _c("run_id", "string"),
            _c("posted_at", "timestamp", comment="when the posting run was requested (Postgres time)"),
        ),
        partitioned_by=("entity_id",),
        append_only=True,
        natural_key=("journal_id", "content_hash"),
        comment="The ERP feed (Stage 7b): one row per posted journal, written after its lines.",
    ),
)

# ---------------------------------------------------------------- ops: the generator's run record and answer key

DATAGEN_TABLES: Final = (
    TableSchema(
        _t(OPS, "datagen_runs"),
        (
            _c("run_id", "string"),
            _c("profile", "string"),
            _c("seed", "bigint"),
            _c("config_hash", "string"),
            _c("start_date", "date"),
            _c("days", "int"),
            _c("generator_version", "string"),
            _c("generated_at", "timestamp"),
        ),
        append_only=True,
    ),
    TableSchema(
        _t(OPS, "datagen_manifest"),
        (
            _c("run_id", "string"),
            _c("table_key", "string"),
            _c("currency", "string", comment="'*' for tables without amounts"),
            _c("row_count", "bigint"),
            _c("amount_total_minor", "bigint", comment="signed control total in minor units"),
            _c("digest", "string"),
        ),
        append_only=True,
        comment="Landing control totals the generator wrote; the gate checks them against the landing tables.",
    ),
    TableSchema(
        _t(OPS, "answer_key_groups"),
        (
            _c("run_id", "string"),
            _c("template", "int"),
            _c("group_key", "string"),
            _c("expected_outcome", "string", comment="auto, suggested or open"),
            _n("break_type", "string"),
            _c("entity_id", "string"),
            _c("currency", "string"),
        ),
        append_only=True,
    ),
    TableSchema(
        _t(OPS, "answer_key_members"),
        (
            _c("run_id", "string"),
            _c("template", "int"),
            _c("group_key", "string"),
            _c("side", "string"),
            _c("source_table", "string"),
            _c("source_key", "string"),
        ),
        append_only=True,
    ),
    TableSchema(
        _t(OPS, "injected_breaks"),
        (
            _c("run_id", "string"),
            _c("break_type", "string"),
            _c("source_table", "string"),
            _c("source_key", "string"),
            _c("detail", "string"),
        ),
        append_only=True,
    ),
)

ALL_TABLES: Final = (*OPS_TABLES, *DATAGEN_TABLES, *REF_TABLES, *LANDING_TABLES, *MATCH_TABLES, *LEDGER_TABLES)
REGISTRY: Final[dict[LogicalTable, TableSchema]] = {t.table: t for t in ALL_TABLES}
if len(REGISTRY) != len(ALL_TABLES):  # pragma: no cover - a typo guard at import time
    raise ValueError("duplicate table in the schema registry")


def schema_of(table: LogicalTable) -> TableSchema:
    return REGISTRY[table]
