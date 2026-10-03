"""Reference normalization: filters exclude, and rows a mapping cannot evaluate are rejected, never dropped."""

from __future__ import annotations

from datetime import date

from close_contracts.binding import bind
from close_contracts.config.data_source import DataSource
from close_contracts.default_sources import col, fn, s
from close_engine.reference.items import normalize


def _source() -> DataSource:
    return DataSource.model_validate(
        {
            "source_id": "fees",
            "description": "fees",
            "landing_table": "landing.psp_fee_invoices",
            "identity_key": ["invoice_id"],
            "filter": fn("ne", col("psp"), s("skip")),
            "mapping": {
                "entity_id": fn(
                    "substring", col("account_id"), {"lit": 1, "type": "bigint"}, {"lit": 4, "type": "bigint"}
                ),
                "currency": col("currency"),
                "amount_minor": fn("parse_bigint", col("period")),
                "transaction_date": col("invoice_date"),
            },
        }
    )


def test_normalize_excludes_rejects_and_keeps() -> None:
    base = {"account_id": "US01-x", "currency": "USD", "invoice_date": date(2026, 7, 1), "psp": "stripe"}
    rows = [
        base | {"invoice_id": "i1", "period": "120"},
        base | {"invoice_id": "i2", "period": "12x"},  # the mapping cannot parse it
        base | {"invoice_id": "i3", "period": "5", "currency": None},  # a required field is null
        base | {"invoice_id": "i4", "period": "5", "psp": "skip"},  # the filter excludes it
    ]
    out = normalize(bind(_source()), rows)
    assert [(i.source_key, i.fields["amount_minor"]) for i in out.items] == [('["i1"]', 120)]
    assert out.excluded == 1
    assert [key for key, _ in out.rejected] == ['["i2"]', '["i3"]']
