"""The table registry, source binding and the default data sources."""

from __future__ import annotations

import pytest

from close_contracts.binding import ITEM_FIELD_TYPES, REQUIRED_FIELDS, bind, dsl_schema, dsl_type
from close_contracts.config.data_source import DataSource
from close_contracts.default_sources import DEFAULT_SOURCES, IDENTITY_KEYS, source_key
from close_contracts.dsl import DslType, DslTypeError
from close_contracts.enums import Schema
from close_contracts.table_schemas import (
    ALL_TABLES,
    ITEM_FIELD_NAMES,
    LANDING_TABLES,
    REGISTRY,
    Column,
    TableSchema,
    schema_of,
)
from close_contracts.tables import LogicalTable, Tables


def test_the_registry_covers_every_logical_table_once() -> None:
    assert len(REGISTRY) == len(ALL_TABLES) == 47  # Stage 7b: ledger.journal_lines and ledger.journal_postings
    for table in Tables.all():
        assert table in REGISTRY
    assert schema_of(Tables.OPS_RUN_MANIFESTS).append_only


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Column("Bad", "string"), "invalid column name"),
        (lambda: Column("x", "double"), "unsupported type"),
        (lambda: TableSchema(LogicalTable(Schema.OPS, "t"), (Column("a", "int"), Column("a", "int"))), "duplicate"),
        (lambda: TableSchema(LogicalTable(Schema.OPS, "t"), (Column("a", "int"),), partitioned_by=("b",)), "partition"),
        (lambda: TableSchema(LogicalTable(Schema.OPS, "t"), (Column("a", "int"),), natural_key=("b",)), "natural key"),
    ],
)
def test_registry_entries_are_validated(build: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()  # type: ignore[operator]


def test_no_stored_column_is_double() -> None:
    assert not [(t.table.key, c.name) for t in ALL_TABLES for c in t.columns if "double" in c.type or "float" in c.type]


def test_dsl_types_of_stored_columns() -> None:
    assert dsl_type("int") == DslType.BIGINT
    assert dsl_type("decimal(19,12)") == DslType.DECIMAL
    assert dsl_type("map<string,string>") is None
    assert "attributes" not in dsl_schema(next(t for t in ALL_TABLES if t.table.key == "ledger.accounting_events"))
    assert set(ITEM_FIELD_TYPES) == set(ITEM_FIELD_NAMES)
    assert set(REQUIRED_FIELDS) <= set(ITEM_FIELD_NAMES)


def test_identity_keys_name_real_columns_of_every_landing_table() -> None:
    assert sorted(IDENTITY_KEYS) == sorted(t.table.key for t in LANDING_TABLES)
    for table in LANDING_TABLES:
        names = {c.name for c in table.columns}
        assert set(IDENTITY_KEYS[table.table.key]) <= names, table.table.key


@pytest.mark.parametrize("source", DEFAULT_SOURCES, ids=lambda s: s.source_id)
def test_every_default_source_binds(source: DataSource) -> None:
    bound = bind(source)
    assert set(REQUIRED_FIELDS) <= set(bound.mapping)


def test_source_keys_are_canonical_json() -> None:
    assert source_key(["po_1", None, 3]) == '["po_1",null,3]'


def _source(**changes: object) -> DataSource:
    base = DEFAULT_SOURCES[0].model_dump(mode="json")
    return DataSource.model_validate({**base, **changes})


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"landing_table": "landing.nope"}, "unknown_table"),
        ({"identity_key": ["no_such_column"]}, "unknown_column"),
        ({"mapping": {"entity_id": {"col": "entity_id"}}}, "missing_field"),
        (
            {"mapping": {**DEFAULT_SOURCES[0].model_dump(mode="json")["mapping"], "not_a_field": {"col": "sku"}}},
            "unknown_field",
        ),
        (
            {"mapping": {**DEFAULT_SOURCES[0].model_dump(mode="json")["mapping"], "amount_minor": {"col": "sku"}}},
            "expected_type",
        ),
        ({"filter": {"col": "sku"}}, "expected_type"),
    ],
)
def test_binding_reports_structured_problems(changes: dict[str, object], code: str) -> None:
    with pytest.raises(DslTypeError) as info:
        bind(_source(**changes))
    assert code in {i.code for i in info.value.issues}, info.value.issues
