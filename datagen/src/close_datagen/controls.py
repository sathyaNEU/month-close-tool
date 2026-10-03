"""Landing control totals: row count and signed amount in minor units, per table and currency.

``expected`` computes them in Python from the generated rows; ``actual`` recomputes them in Spark from the
written Delta tables with separate parsing code. The generator records the expected totals in
``ops.datagen_manifest`` and fails if the two ever differ.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from pyspark.sql import Column, SparkSession
from pyspark.sql import functions as F

from close_contracts.resolver import TableResolver
from close_contracts.table_schemas import REGISTRY
from close_engine.io.tables import read_table

Totals = dict[tuple[str, str], tuple[int, int]]  # (table, currency) -> (rows, amount in minor units)


def _minor(text: object) -> int:
    return int(Decimal(str(text)).scaleb(2)) if text not in (None, "") else 0


def _bank(row: dict[str, Any]) -> int:
    text = str(row["amount_text"])
    minor = int(text) if row["format"] == "bai2" else _minor(text.replace(",", "."))
    return -minor if row["credit_debit"] == "D" else minor


def _python_amount(table: str, row: dict[str, Any]) -> tuple[str, int]:
    simple = {
        "landing.orders": ("currency", "card_amount_minor"),
        "landing.shipping_labels": ("currency", "quoted_minor"),
        "landing.carrier_invoice_lines": ("currency", "billed_minor"),
        "landing.psp_fee_invoices": ("currency", "amount_minor"),
        "landing.disputes": ("currency", "amount_minor"),
        "landing.seller_payables": ("currency", "amount_minor"),
        "landing.tax_remittances": ("currency", "amount_minor"),
        "landing.stored_value_ledger": ("currency", "amount_minor"),
    }
    if table in simple:
        currency, amount = simple[table]
        return str(row[currency]), int(row[amount])
    if table in {"landing.stripe_balance_transactions", "landing.stripe_payouts"}:
        return str(row["currency"]).upper(), int(row["amount"])
    if table == "landing.adyen_settlement_details":
        return str(row["net_currency"]), _minor(row["net_credit"]) - _minor(row["net_debit"])
    if table in {"landing.payout_items", "landing.payout_funding"}:
        return str(row["currency"]), _minor(row["amount"])
    if table == "landing.bank_statement_lines":
        return str(row["currency"]), _bank(row)
    if table == "landing.tax_documents":
        return str(row["currency"]), _minor(row["total_tax"])
    if table == "landing.order_events":
        return "*", int(row["amount_minor"] or 0)
    return "*", 0


def accumulate(into: dict[tuple[str, str], list[int]], table: str, rows: list[dict[str, Any]]) -> None:
    """Add one batch of a landing table's rows to running control totals."""
    if not table.startswith("landing."):
        return
    for row in rows:
        currency, amount = _python_amount(table, row)
        into[(table, currency)][0] += 1
        into[(table, currency)][1] += amount


def running() -> dict[tuple[str, str], list[int]]:
    return defaultdict(lambda: [0, 0])


def expected(rows: dict[str, list[dict[str, Any]]]) -> Totals:
    out = running()
    for table, table_rows in rows.items():
        accumulate(out, table, table_rows)
    return {k: (v[0], v[1]) for k, v in out.items()}


def _dec_minor(name: str) -> Column:
    return F.coalesce((F.col(name).cast("decimal(38,12)") * F.lit(100)).cast("bigint"), F.lit(0).cast("bigint"))


def _spark_columns(table: str) -> tuple[Column, Column]:
    if table in {"landing.stripe_balance_transactions", "landing.stripe_payouts"}:
        return F.upper("currency"), F.col("amount")
    if table == "landing.adyen_settlement_details":
        return F.col("net_currency"), _dec_minor("net_credit") - _dec_minor("net_debit")
    if table in {"landing.payout_items", "landing.payout_funding"}:
        return F.col("currency"), _dec_minor("amount")
    if table == "landing.tax_documents":
        return F.col("currency"), _dec_minor("total_tax")
    if table == "landing.bank_statement_lines":
        unsigned = F.when(F.col("format") == "bai2", F.col("amount_text").cast("bigint")).otherwise(
            (F.regexp_replace("amount_text", ",", ".").cast("decimal(38,12)") * F.lit(100)).cast("bigint")
        )
        return F.col("currency"), F.when(F.col("credit_debit") == "D", -unsigned).otherwise(unsigned)
    if table == "landing.order_events":
        return F.lit("*"), F.coalesce(F.col("amount_minor"), F.lit(0).cast("bigint"))
    columns = {c.name for c in next(s for t, s in REGISTRY.items() if t.key == table).columns}
    for amount in ("card_amount_minor", "quoted_minor", "billed_minor", "amount_minor"):
        if amount in columns:
            return F.col("currency"), F.col(amount)
    return F.lit("*"), F.lit(0).cast("bigint")


def actual(spark: SparkSession, resolver: TableResolver, tables: list[str]) -> Totals:
    out: Totals = {}
    for key in tables:
        table = next(t for t in REGISTRY if t.key == key)
        currency, amount = _spark_columns(key)
        grouped = (
            read_table(spark, resolver, table)
            .select(currency.alias("currency"), amount.alias("amount"))
            .groupBy("currency")
            .agg(F.count(F.lit(1)).alias("rows"), F.sum("amount").alias("amount"))
            .collect()
        )
        for row in grouped:
            out[(key, str(row["currency"]))] = (int(row["rows"]), int(row["amount"] or 0))
    return out
