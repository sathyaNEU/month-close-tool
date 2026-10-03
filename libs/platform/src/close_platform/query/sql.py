"""Render a QueryPlan as parameterised SQL, for a Databricks SQL warehouse (Stage 10 decides whether
production reads go through one; ADR-0005).

Every value a caller supplied, including the entity scope and the page size, travels as a named
parameter marker, so no caller data ever appears in the SQL text. Identifiers come only from the
table registry (column names matching ``^[a-z][a-z0-9_]*$``) and from a TableResolver, and are
backticked. The entity predicate is always the first top-level conjunct of the WHERE clause. A table
version (from a signed cursor or the backend, never from the caller) is rendered as an integer, because
``VERSION AS OF`` takes no parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from close_contracts.query import Aggregate, Eq, Filter, In, Prefix, Range, Scalar, Sort
from close_platform.query.engine import QueryPlan


@dataclass(frozen=True)
class RenderedSQL:
    text: str
    params: dict[str, Scalar] = field(default_factory=dict)


def _q(name: str) -> str:
    return f"`{name}`"


class _Binder:
    def __init__(self) -> None:
        self.params: dict[str, Scalar] = {}

    def __call__(self, value: Scalar) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f":{name}"


def _filter_sql(f: Filter, bind: _Binder) -> str:
    col = _q(f.column)
    match f:
        case Eq(_, value):
            return f"{col} = {bind(value)}"
        case In(_, values):
            return f"{col} IN ({', '.join(bind(v) for v in values)})"
        case Range(_, low, high):
            parts = [
                f"{col} >= {bind(low)}" if low is not None else None,
                f"{col} < {bind(high)}" if high is not None else None,
            ]
            return " AND ".join(p for p in parts if p is not None)
        case Prefix(_, prefix):
            return f"startswith({col}, {bind(prefix)})"
    raise TypeError(f"unknown filter {f!r}")  # pragma: no cover (Filter is closed)


def _after_sql(sort: tuple[Sort, ...], after: tuple[Scalar, ...], bind: _Binder) -> str:
    clauses = []
    for i, s in enumerate(sort):
        ties = [f"{_q(sort[j].column)} = {bind(after[j])}" for j in range(i)]
        beyond = f"{_q(s.column)} {'<' if s.descending else '>'} {bind(after[i])}"
        clauses.append("(" + " AND ".join([*ties, beyond]) + ")")
    return " OR ".join(clauses)


def _aggregate_sql(a: Aggregate) -> str:
    if a.function == "count":
        inner = "*" if a.column is None else _q(a.column)
        return f"COUNT({inner}) AS {_q(a.name)}"
    return f"SUM({_q(a.column or '')}) AS {_q(a.name)}"


def render(plan: QueryPlan, identifier: str) -> RenderedSQL:
    spec, table = plan.spec, plan.table
    bind = _Binder()
    entity = f"{_q(table.entity_column)} IN ({', '.join(bind(e) for e in sorted(plan.entities))})"
    conditions = [f"({entity})", *(f"({_filter_sql(f, bind)})" for f in spec.filters)]
    if plan.after is not None and not spec.grouped:
        conditions.append(f"({_after_sql(spec.sort, plan.after, bind)})")
    source = identifier if plan.version is None else f"{identifier} VERSION AS OF {int(plan.version)}"
    where = " AND ".join(conditions)
    if spec.grouped:
        select = ", ".join([*(_q(c) for c in spec.group_by), *(_aggregate_sql(a) for a in spec.aggregates)])
        text = f"SELECT {select} FROM {source} WHERE {where}"  # nosec B608 (values are bound markers)
        if spec.group_by:
            group = ", ".join(_q(c) for c in spec.group_by)
            text += f" GROUP BY {group} ORDER BY " + ", ".join(f"{_q(c)} ASC" for c in spec.group_by)
    else:
        select = ", ".join(_q(c) for c in spec.columns)
        order = ", ".join(f"{_q(s.column)} {'DESC' if s.descending else 'ASC'}" for s in spec.sort)
        text = f"SELECT {select} FROM {source} WHERE {where} ORDER BY {order}"  # nosec B608 (values are bound markers)
    text += f" LIMIT {bind(plan.fetch)}"
    return RenderedSQL(text, bind.params)
