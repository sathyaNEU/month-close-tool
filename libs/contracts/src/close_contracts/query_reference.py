"""The reference evaluator: the query semantics of :mod:`close_contracts.query`, in plain Python.

It is deliberately simple and slow. Its job is to be obviously right, so the DataFrame backend and
the SQL renderer can be tested against it on thousands of generated specs, and so the app's tests
can run without Spark.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping, Sequence

from close_contracts.query import (
    INT64_MAX,
    INT64_MIN,
    Eq,
    Filter,
    In,
    Prefix,
    QueryableTable,
    QueryError,
    QuerySpec,
    Range,
    Row,
    Scalar,
    Sort,
    validate,
)


def _matches(row: Mapping[str, Scalar | None], f: Filter) -> bool:
    value = row.get(f.column)
    if value is None:
        return False  # a filter never matches NULL
    match f:
        case Eq(_, expected):
            return value == expected
        case In(_, values):
            return value in values
        case Range(_, low, high):
            return (low is None or value >= low) and (high is None or value < high)  # type: ignore[operator]
        case Prefix(_, prefix):
            return isinstance(value, str) and value.startswith(prefix)
    raise QueryError(f"unknown filter {f!r}")  # pragma: no cover (Filter is closed)


def compare_keys(a: Sequence[Scalar], b: Sequence[Scalar], sort: Sequence[Sort]) -> int:
    """-1, 0 or 1 comparing two sort-key tuples under the sort's directions."""
    for x, y, s in zip(a, b, sort, strict=True):
        if x != y:
            less = x < y  # type: ignore[operator]
            return (1 if less else -1) if s.descending else (-1 if less else 1)
    return 0


def sort_key(row: Mapping[str, Scalar | None], sort: Sequence[Sort]) -> tuple[Scalar, ...]:
    return tuple(row[s.column] for s in sort)  # type: ignore[misc]


def evaluate(
    spec: QuerySpec,
    table: QueryableTable,
    rows: Iterable[Mapping[str, Scalar | None]],
    *,
    entities: frozenset[str],
    after: tuple[Scalar, ...] | None = None,
    fetch: int | None = None,
) -> list[Row]:
    """Run ``spec`` over ``rows`` for a caller who may see ``entities``. Row queries return rows
    strictly after ``after`` (a previous page's last sort key); ``fetch`` defaults to the limit."""
    validate(spec, table)
    n = spec.limit if fetch is None else fetch
    visible = [r for r in rows if r.get(table.entity_column) in entities and all(_matches(r, f) for f in spec.filters)]
    if spec.grouped:
        return _grouped(spec, visible)[:n]
    if after is not None:
        visible = [r for r in visible if compare_keys(sort_key(r, spec.sort), after, spec.sort) > 0]

    def cmp(r1: Mapping[str, Scalar | None], r2: Mapping[str, Scalar | None]) -> int:
        return compare_keys(sort_key(r1, spec.sort), sort_key(r2, spec.sort), spec.sort)

    ordered = sorted(visible, key=functools.cmp_to_key(cmp))
    return [{c: r.get(c) for c in spec.columns} for r in ordered[:n]]


def _grouped(spec: QuerySpec, rows: list[Mapping[str, Scalar | None]]) -> list[Row]:
    groups: dict[tuple[Scalar | None, ...], list[Mapping[str, Scalar | None]]] = {}
    for r in rows:
        groups.setdefault(tuple(r.get(c) for c in spec.group_by), []).append(r)
    if not spec.group_by and not groups:
        groups[()] = []  # a global aggregate over no rows still returns one row
    out: list[Row] = []
    for key in sorted(groups):
        members = groups[key]
        result: Row = dict(zip(spec.group_by, key, strict=True))
        for a in spec.aggregates:
            if a.function == "count":
                result[a.name] = (
                    len(members) if a.column is None else sum(1 for m in members if m.get(a.column) is not None)
                )
            else:
                values = [m.get(a.column) for m in members if a.column is not None and m.get(a.column) is not None]
                total = sum(v for v in values if isinstance(v, int))
                if not INT64_MIN <= total <= INT64_MAX:
                    raise QueryError(f"sum {a.name!r} overflows 64 bits")  # Spark with ANSI mode raises too
                result[a.name] = total if values else None
        out.append(result)
    return out
