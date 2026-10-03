"""The tables the read path may query, with the columns it may see. A spec naming anything else is
rejected before it reaches Spark, so this registry is the read path's allowlist. Each definition
must match the table the engine writes (a test compares them)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from close_contracts.query import Column, QueryableTable, QueryError
from close_contracts.tables import Tables

SMOKE_RESULTS: Final = QueryableTable(
    table=Tables.OPS_SMOKE_RESULTS,
    columns=(
        Column("run_id", "string"),
        Column("seq", "bigint"),
        Column("row_id", "string"),
        Column("entity_id", "string"),
        Column("currency", "string"),
        Column("amount_minor", "bigint"),
        Column("amount_eur_minor", "bigint"),
    ),
    entity_column="entity_id",
    unique_key=("row_id",),
)

RUN_MANIFESTS: Final = QueryableTable(
    table=Tables.OPS_RUN_MANIFESTS,
    columns=(
        Column("run_id", "string"),
        Column("attempt", "bigint"),
        Column("fencing_token", "bigint"),
        Column("job", "string"),
        Column("entity_id", "string"),
        Column("row_count", "bigint"),
        Column("digest", "string"),
    ),
    entity_column="entity_id",
    # One row per writer and entity. A repeated identical row is tolerated (the table is append-only);
    # two different digests for the same key are an integrity error the reader must surface.
    unique_key=("run_id", "attempt", "fencing_token", "entity_id"),
)

JOURNAL_LINES: Final = QueryableTable(  # Stage 7b: the trial balance and drill-down
    table=Tables.LEDGER_JOURNAL_LINES,
    columns=(
        Column("journal_id", "string"),
        Column("entity_id", "string"),
        Column("number", "bigint"),
        Column("line_no", "bigint"),
        Column("period", "string"),
        Column("account", "string"),
        Column("side", "string"),
        Column("amount_minor", "bigint"),
        Column("currency", "string"),
        Column("debit_minor", "bigint"),
        Column("credit_minor", "bigint"),
        Column("line_description", "string"),
        Column("journal_description", "string"),
        Column("source", "string"),
        Column("run_id", "string"),
        Column("functional_amount_minor", "bigint"),  # Stage 7c: the line in its entity's functional currency
        Column("functional_currency", "string"),
        Column("line_kind", "string", nullable=True),  # entry or fx_rounding; null on lines posted before 7c
    ),
    entity_column="entity_id",
    unique_key=("journal_id", "line_no"),  # a journal posts once, so its lines are unique
)

JOURNAL_POSTINGS: Final = QueryableTable(  # Stage 7b: the ERP feed the stub ERP reads
    table=Tables.LEDGER_JOURNAL_POSTINGS,
    columns=(
        Column("journal_id", "string"),
        Column("entity_id", "string"),
        Column("number", "bigint"),
        Column("period", "string"),
        Column("content_hash", "string"),
        Column("payload", "string"),
        Column("run_id", "string"),
    ),
    entity_column="entity_id",
    unique_key=("journal_id",),
)

GL_ROLLUP_LINES: Final = QueryableTable(  # Stage 8a: the engine's journals, summarized, for balances and roll-forward
    table=Tables.LEDGER_GL_ROLLUP_LINES,
    columns=(
        Column("rollup_line_id", "string"),
        Column("entity_id", "string"),
        Column("period", "string"),
        Column("account", "string"),
        Column("dimensions_key", "string"),
        Column("currency", "string"),
        Column("debit_minor", "bigint"),
        Column("credit_minor", "bigint"),
        Column("functional_debit_minor", "bigint", nullable=True),
        Column("functional_credit_minor", "bigint", nullable=True),
        Column("line_count", "bigint"),
        Column("run_id", "string"),
    ),
    entity_column="entity_id",
    unique_key=("rollup_line_id",),
)

QUERYABLE: Final[Mapping[str, QueryableTable]] = {
    t.key: t for t in (SMOKE_RESULTS, RUN_MANIFESTS, JOURNAL_LINES, JOURNAL_POSTINGS, GL_ROLLUP_LINES)
}


def queryable(key: str) -> QueryableTable:
    try:
        return QUERYABLE[key]
    except KeyError:
        raise QueryError(f"{key!r} is not a queryable table") from None
