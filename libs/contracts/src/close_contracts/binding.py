"""Binding a data source: typecheck its filter and mapping against the landing and canonical schemas.

A data source is only usable once every mapped expression types against the landing table's columns and
produces the canonical field's type, the required fields are all mapped, and the identity key names real
columns. Binding returns the IR the normalizer runs (Stage 3), or raises with every problem at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from close_contracts.config.data_source import DataSource
from close_contracts.dsl.ast import DslType
from close_contracts.dsl.ir import Ir
from close_contracts.dsl.typecheck import DslTypeError, Issue, typecheck
from close_contracts.table_schemas import MATCH_TABLES, REGISTRY, TableSchema

_DSL_TYPES: Final = {
    "string": DslType.STRING,
    "bigint": DslType.BIGINT,
    "int": DslType.BIGINT,
    "boolean": DslType.BOOLEAN,
    "date": DslType.DATE,
    "timestamp": DslType.TIMESTAMP,
}
REQUIRED_FIELDS: Final = ("entity_id", "currency", "amount_minor", "transaction_date")


def dsl_type(spark_type: str) -> DslType | None:
    """The DSL type of a stored column type (None for types the DSL cannot read, such as maps)."""
    if spark_type.startswith("decimal("):
        return DslType.DECIMAL
    return _DSL_TYPES.get(spark_type)


def dsl_schema(schema: TableSchema) -> dict[str, DslType]:
    out = {}
    for column in schema.columns:
        type_ = dsl_type(column.type)
        if type_ is not None:
            out[column.name] = type_
    return out


def _item_field_types() -> dict[str, DslType]:
    versions = next(t for t in MATCH_TABLES if t.table.name == "item_versions")
    from close_contracts.table_schemas import ITEM_FIELD_NAMES

    types = dsl_schema(versions)
    return {name: types[name] for name in ITEM_FIELD_NAMES}


ITEM_FIELD_TYPES: Final = _item_field_types()


@dataclass(frozen=True)
class BoundSource:
    source: DataSource
    mapping: dict[str, Ir]
    filter: Ir | None


def bind(source: DataSource) -> BoundSource:
    table = next((t for t in REGISTRY if t.key == source.landing_table), None)
    if table is None:
        raise DslTypeError([Issue("unknown_table", "/landing_table", f"{source.landing_table} is not registered")])
    columns = dsl_schema(REGISTRY[table])
    issues: list[Issue] = []
    missing_key = [c for c in source.identity_key if c not in columns]
    if missing_key:
        issues.append(Issue("unknown_column", "/identity_key", f"identity key columns not in the table: {missing_key}"))
    missing = [f for f in REQUIRED_FIELDS if f not in source.mapping]
    if missing:
        issues.append(Issue("missing_field", "/mapping", f"required fields are not mapped: {missing}"))
    mapping: dict[str, Ir] = {}
    for field, expr in source.mapping.items():
        expected = ITEM_FIELD_TYPES.get(field)
        if expected is None:
            issues.append(Issue("unknown_field", f"/mapping/{field}", f"{field} is not a canonical item field"))
            continue
        try:
            mapping[field] = typecheck(expr, columns, expect=expected)
        except DslTypeError as exc:
            issues.extend(Issue(i.code, f"/mapping/{field}{i.path}", i.message) for i in exc.issues)
    filter_ir = None
    if source.filter is not None:
        try:
            filter_ir = typecheck(source.filter, columns, expect=DslType.BOOLEAN)
        except DslTypeError as exc:
            issues.extend(Issue(i.code, f"/filter{i.path}", i.message) for i in exc.issues)
    if issues:
        raise DslTypeError(issues)
    return BoundSource(source, mapping, filter_ir)
