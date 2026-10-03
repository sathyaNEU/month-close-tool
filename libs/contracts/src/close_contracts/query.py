"""Typed read queries: the read path's contract (ADR-0005, final format in ADR-0009).

A :class:`QuerySpec` says what to read from one registered table: which columns, which filters (all
ANDed together), which order, or which grouping with ``count``/``sum`` aggregates, and how many rows.
It carries no SQL and no entity scope. The query engine adds the caller's entity filter to every
query it runs, so no spec can widen what a user sees, and every spec is checked against its table's
:class:`QueryableTable` definition before anything runs.

Three executors implement exactly the semantics below and the tests hold them to each other: the
DataFrame backend (over Spark Connect in the app), the SQL renderer (for a Databricks SQL warehouse),
and :func:`close_contracts.query_reference.evaluate`, a pure-Python oracle.

Semantics:

* Values are ``string`` or ``bigint`` (64-bit signed). Strings compare by code point, which is the
  same order as Spark's binary UTF-8 order.
* A filter never matches NULL. ``Range`` is half-open (``low <= x < high``, either bound optional).
  ``Prefix`` is ``startswith``, so there are no pattern characters to escape.
* Row queries are ordered by ``sort``, which must end with the table's unique key, so the order is
  total and a cursor (the rows strictly after the previous page's last row) never skips or repeats a
  row. Every sort column is also a projected column, so the cursor can be built from the page.
* Grouped queries return one row per group, ordered by the group columns ascending, at most
  ``MAX_GROUPS`` of them (more is an error, never a silent cut). ``count`` counts rows (or non-null
  values of a column) and ``sum`` adds a bigint column exactly. A global aggregate over no rows gives
  one row with ``count`` 0 and ``sum`` NULL, as SQL does.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final, Literal

from close_contracts.tables import LogicalTable

ColumnType = Literal["string", "bigint"]
Scalar = str | int
Row = dict[str, Scalar | None]

MAX_PAGE: Final = 500
MAX_GROUPS: Final = 1000
MAX_IN: Final = 200
MAX_STRING: Final = 1024
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1
_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class QueryError(ValueError):
    """The spec is not valid for its table (an API caller gets a 400)."""


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    nullable: bool = False

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid column name {self.name!r}")


@dataclass(frozen=True)
class QueryableTable:
    """What the read path may see of one table: its columns, the entity column that scopes every
    query, and the unique key that ends every row order."""

    table: LogicalTable
    columns: tuple[Column, ...]
    entity_column: str
    unique_key: tuple[str, ...]

    def __post_init__(self) -> None:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.table.key}: duplicate columns")
        entity = self._get(self.entity_column)
        if entity is None or entity.type != "string" or entity.nullable:
            raise ValueError(f"{self.table.key}: the entity column must be a non-nullable string column")
        if not self.unique_key:
            raise ValueError(f"{self.table.key}: a unique key is required")
        for name in self.unique_key:
            col = self._get(name)
            if col is None or col.nullable:
                raise ValueError(f"{self.table.key}: unique key column {name!r} must exist and be non-nullable")

    def _get(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def key(self) -> str:
        return self.table.key

    def column(self, name: str) -> Column:
        col = self._get(name)
        if col is None:
            raise QueryError(f"{self.key} has no column {name!r}")
        return col


@dataclass(frozen=True)
class Eq:
    column: str
    value: Scalar


@dataclass(frozen=True)
class In:
    column: str
    values: tuple[Scalar, ...]


@dataclass(frozen=True)
class Range:
    column: str
    low: Scalar | None = None
    high: Scalar | None = None


@dataclass(frozen=True)
class Prefix:
    column: str
    prefix: str


Filter = Eq | In | Range | Prefix


@dataclass(frozen=True)
class Sort:
    column: str
    descending: bool = False


@dataclass(frozen=True)
class Aggregate:
    function: Literal["count", "sum"]
    name: str
    column: str | None = None  # count(*) when None; sum needs a bigint column


@dataclass(frozen=True)
class QuerySpec:
    table: str
    columns: tuple[str, ...] = ()
    filters: tuple[Filter, ...] = ()
    sort: tuple[Sort, ...] = ()
    group_by: tuple[str, ...] = ()
    aggregates: tuple[Aggregate, ...] = ()
    limit: int = 100

    @property
    def grouped(self) -> bool:
        return bool(self.aggregates)

    @property
    def output_columns(self) -> tuple[str, ...]:
        return (*self.group_by, *(a.name for a in self.aggregates)) if self.grouped else self.columns

    def fingerprint(self) -> str:
        """SHA-256 of the spec's canonical description, the identity a cursor is bound to."""
        return hashlib.sha256(json.dumps(_describe(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def typed(value: Scalar | None) -> list[str] | None:
    """A value as a JSON-safe tagged pair (bigints as strings, so they survive any JSON parser)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise QueryError(f"unsupported value {value!r}")
    return ["s", value] if isinstance(value, str) else ["i", str(value)]


def untyped(pair: object) -> Scalar | None:
    if pair is None:
        return None
    if isinstance(pair, list) and len(pair) == 2 and pair[0] in {"s", "i"} and isinstance(pair[1], str):
        return pair[1] if pair[0] == "s" else int(pair[1])
    raise QueryError(f"malformed value {pair!r}")


def _describe(spec: QuerySpec) -> dict[str, object]:
    def flt(f: Filter) -> list[object]:
        match f:
            case Eq(column, value):
                return ["eq", column, typed(value)]
            case In(column, values):
                return ["in", column, [typed(v) for v in values]]
            case Range(column, low, high):
                return ["range", column, typed(low), typed(high)]
            case Prefix(column, prefix):
                return ["prefix", column, prefix]
        raise QueryError(f"unknown filter {f!r}")  # pragma: no cover (Filter is closed)

    return {
        "table": spec.table,
        "columns": list(spec.columns),
        "filters": [flt(f) for f in spec.filters],
        "sort": [[s.column, s.descending] for s in spec.sort],
        "group_by": list(spec.group_by),
        "aggregates": [[a.function, a.name, a.column] for a in spec.aggregates],
        "limit": spec.limit,
    }


def check_value(col: Column, value: object, what: str) -> None:
    if col.type == "bigint":
        if isinstance(value, bool) or not isinstance(value, int) or not INT64_MIN <= value <= INT64_MAX:
            raise QueryError(f"{what}: {col.name} needs a 64-bit integer, got {value!r}")
        return
    if not isinstance(value, str):
        raise QueryError(f"{what}: {col.name} needs a string, got {value!r}")
    if len(value) > MAX_STRING:
        raise QueryError(f"{what}: {col.name} values are limited to {MAX_STRING} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise QueryError(f"{what}: {col.name} value is not valid Unicode") from None


def _check_filter(t: QueryableTable, f: Filter) -> None:
    col = t.column(f.column)
    match f:
        case Eq(_, value):
            check_value(col, value, "eq")
        case In(_, values):
            if not 1 <= len(values) <= MAX_IN:
                raise QueryError(f"in: {col.name} takes 1 to {MAX_IN} values")
            for v in values:
                check_value(col, v, "in")
        case Range(_, low, high):
            if low is None and high is None:
                raise QueryError(f"range: {col.name} needs a low or a high bound")
            for bound in (low, high):
                if bound is not None:
                    check_value(col, bound, "range")
        case Prefix(_, prefix):
            if col.type != "string":
                raise QueryError(f"prefix: {col.name} is not a string column")
            check_value(col, prefix, "prefix")
            if not prefix:
                raise QueryError(f"prefix: {col.name} needs a non-empty prefix")
        case _:  # pragma: no cover (Filter is closed)
            raise QueryError(f"unknown filter {f!r}")


def validate(spec: QuerySpec, t: QueryableTable) -> None:
    """Raise QueryError unless ``spec`` is a well-formed query over ``t``."""
    if spec.table != t.key:
        raise QueryError(f"spec is for {spec.table!r}, not {t.key!r}")
    if isinstance(spec.limit, bool) or not isinstance(spec.limit, int) or spec.limit < 1:
        raise QueryError("limit must be a positive integer")
    for f in spec.filters:
        _check_filter(t, f)
    if spec.grouped:
        _validate_grouped(spec, t)
    else:
        _validate_rows(spec, t)


def _validate_rows(spec: QuerySpec, t: QueryableTable) -> None:
    if spec.group_by:
        raise QueryError("group_by needs at least one aggregate")
    if not spec.columns or len(set(spec.columns)) != len(spec.columns):
        raise QueryError("a row query needs distinct columns")
    for name in spec.columns:
        t.column(name)
    if spec.limit > MAX_PAGE:
        raise QueryError(f"pages hold at most {MAX_PAGE} rows")
    sort_cols = [s.column for s in spec.sort]
    if len(set(sort_cols)) != len(sort_cols):
        raise QueryError("a column may appear only once in sort")
    if tuple(sort_cols[-len(t.unique_key) :]) != t.unique_key:
        raise QueryError(f"sort must end with the unique key {list(t.unique_key)}")
    for name in sort_cols:
        if t.column(name).nullable:
            raise QueryError(f"sort column {name!r} is nullable")
        if name not in spec.columns:
            raise QueryError(f"sort column {name!r} must also be a selected column")


def _validate_grouped(spec: QuerySpec, t: QueryableTable) -> None:
    if spec.columns or spec.sort:
        raise QueryError("a grouped query has no columns or sort (it is ordered by its group columns)")
    if len(set(spec.group_by)) != len(spec.group_by):
        raise QueryError("group_by columns must be distinct")
    for name in spec.group_by:
        if t.column(name).nullable:
            raise QueryError(f"group_by column {name!r} is nullable")
    if spec.limit > MAX_GROUPS:
        raise QueryError(f"grouped results hold at most {MAX_GROUPS} groups")
    names = [a.name for a in spec.aggregates]
    if len(set(names)) != len(names) or set(names) & set(spec.group_by):
        raise QueryError("aggregate names must be distinct and differ from the group columns")
    for a in spec.aggregates:
        if not _NAME_RE.fullmatch(a.name):
            raise QueryError(f"invalid aggregate name {a.name!r}")
        if a.function == "count":
            if a.column is not None:
                t.column(a.column)
        elif a.function == "sum":
            if a.column is None or t.column(a.column).type != "bigint":
                raise QueryError(f"sum {a.name!r} needs a bigint column")
        else:
            raise QueryError(f"unknown aggregate {a.function!r}")
