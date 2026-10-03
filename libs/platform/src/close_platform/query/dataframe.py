"""A QueryBackend on the DataFrame API. It uses only what Spark Connect and a classic session share,
so the app runs it over Spark Connect and the tests run the same code on a local session.

Every read is pinned to the plan's table version (``versionAsOf``), so all pages of one paging
session see the same snapshot. Column names come from the table registry, values travel as literals
built by ``F.lit`` (never as SQL text), and the entity filter is always the first condition.
"""

from __future__ import annotations

import functools
import operator
from collections.abc import Callable
from typing import Any, TypeVar

from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F

from close_contracts.query import Aggregate, Eq, Filter, In, Prefix, QueryableTable, Range, Row, Scalar, Sort
from close_contracts.resolver import PathTableResolver, TableResolver
from close_platform.query.engine import QueryPlan, TableMissingError
from close_platform.query.history import MissingTableError, latest_version

T = TypeVar("T")
SessionRunner = Callable[[Callable[[SparkSession], Any]], Any]
"""Runs one unit of work with a session. The Spark Connect pool adds concurrency limits, tags and
timeouts; tests pass ``lambda work: work(spark)``."""


def filter_column(f: Filter) -> Column:
    col = F.col(f.column)
    match f:
        case Eq(_, value):
            return col == F.lit(value)
        case In(_, values):
            return col.isin(list(values))
        case Range(_, low, high):
            parts = [
                (col >= F.lit(low)) if low is not None else None,
                (col < F.lit(high)) if high is not None else None,
            ]
            return functools.reduce(operator.and_, [p for p in parts if p is not None])
        case Prefix(_, prefix):
            return col.startswith(prefix)
    raise TypeError(f"unknown filter {f!r}")  # pragma: no cover (Filter is closed)


def after_column(sort: tuple[Sort, ...], after: tuple[Scalar, ...]) -> Column:
    """Rows strictly after ``after`` in ``sort`` order: (k1 > a1) OR (k1 = a1 AND k2 > a2) OR ...,
    with the comparison flipped for descending keys."""
    clauses = []
    for i, s in enumerate(sort):
        col = F.col(s.column)
        beyond = col < F.lit(after[i]) if s.descending else col > F.lit(after[i])
        ties = [F.col(sort[j].column) == F.lit(after[j]) for j in range(i)]
        clauses.append(functools.reduce(operator.and_, [*ties, beyond]))
    return functools.reduce(operator.or_, clauses)


def aggregate_column(a: Aggregate) -> Column:
    if a.function == "count":
        return (F.count(F.lit(1)) if a.column is None else F.count(F.col(a.column))).alias(a.name)
    if a.column is None:  # validation guarantees a column for sum
        raise TypeError(f"sum {a.name!r} has no column")
    return F.sum(F.col(a.column)).alias(a.name)


def compile_plan(df: DataFrame, plan: QueryPlan) -> DataFrame:
    spec, table = plan.spec, plan.table
    condition = F.col(table.entity_column).isin(sorted(plan.entities))
    for f in spec.filters:
        condition = condition & filter_column(f)
    if spec.grouped:
        aggregates = [aggregate_column(a) for a in spec.aggregates]
        filtered = df.where(condition)
        if not spec.group_by:
            return filtered.agg(*aggregates).limit(plan.fetch)
        return (
            filtered.groupBy(*spec.group_by)
            .agg(*aggregates)
            .orderBy(*[F.col(c).asc() for c in spec.group_by])
            .limit(plan.fetch)
        )
    if plan.after is not None:
        condition = condition & after_column(spec.sort, plan.after)
    order = [F.col(s.column).desc() if s.descending else F.col(s.column).asc() for s in spec.sort]
    return df.where(condition).select(*spec.columns).orderBy(*order).limit(plan.fetch)


def read_table(session: SparkSession, resolver: TableResolver, table: QueryableTable, version: int | None) -> DataFrame:
    reader = session.read.format("delta")
    if version is not None:
        reader = reader.option("versionAsOf", version)
    if isinstance(resolver, PathTableResolver):
        return reader.load(resolver.location(table.table))
    return reader.table(resolver.table(table.table))


class DataFrameBackend:
    def __init__(self, run: SessionRunner, resolver: TableResolver) -> None:
        self._run = run
        self._resolver = resolver

    def current_version(self, table: QueryableTable) -> int | None:
        try:
            version: int = self._run(lambda session: latest_version(session, self._resolver, table.table))
        except MissingTableError:
            raise TableMissingError(table.key) from None
        return version

    def fetch(self, plan: QueryPlan) -> list[Row]:
        def work(session: SparkSession) -> list[Row]:
            df = compile_plan(read_table(session, self._resolver, plan.table, plan.version), plan)
            rows: list[Row] = df.toArrow().to_pylist()
            return rows

        result: list[Row] = self._run(work)
        return result
