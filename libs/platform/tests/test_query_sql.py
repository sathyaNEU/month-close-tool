"""The SQL renderer's safety properties over generated specs (Gate 1b): no caller value ever appears
in the SQL text, markers and bindings match one to one, every statement parses as Databricks SQL, and
the entity predicate is always a top-level conjunct. 10,000 examples run in the slow variant."""

from __future__ import annotations

import re

import pytest
import sqlglot
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from querykit import TABLE, grouped_specs, row_specs
from sqlglot import exp

from close_contracts.query import Eq, Filter, In, Prefix, QuerySpec, Range, Scalar, Sort
from close_platform.query.engine import QueryPlan
from close_platform.query.sql import render

IDENTIFIER = "delta.`s3a://close-lake/local/ops/smoke_results`"
_MARKER = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


@st.composite
def sentinel_strings(draw: st.DrawFn) -> str:
    return "zq" + draw(st.text(alphabet="0123456789abcdef", min_size=10, max_size=10))


sentinel_ints = st.integers(10**12, 10**15)  # long enough never to appear inside a marker name or keyword


def _with_sentinels(f: Filter, draw: st.DrawFn) -> Filter:
    def value() -> Scalar:
        return (
            draw(sentinel_ints) if f.column in {"seq", "amount_minor", "amount_eur_minor"} else draw(sentinel_strings())
        )

    match f:
        case Eq(column, _):
            return Eq(column, value())
        case In(column, values):
            return In(column, tuple(value() for _ in values))
        case Range(column, low, high):
            return Range(column, None if low is None else value(), None if high is None else value())
        case Prefix(column, _):
            return Prefix(column, draw(sentinel_strings()))
    raise TypeError(f)  # pragma: no cover


@st.composite
def sentinel_plans(draw: st.DrawFn) -> tuple[QueryPlan, set[str]]:
    spec = draw(st.one_of(row_specs(), grouped_specs()))
    spec = QuerySpec(
        spec.table,
        spec.columns,
        tuple(_with_sentinels(f, draw) for f in spec.filters),
        spec.sort,
        spec.group_by,
        spec.aggregates,
        spec.limit,
    )
    entities = frozenset(draw(st.lists(sentinel_strings(), min_size=1, max_size=3)))
    after = None
    if not spec.grouped and draw(st.booleans()):
        after = tuple(
            draw(sentinel_ints) if s.column in {"seq", "amount_minor", "amount_eur_minor"} else draw(sentinel_strings())
            for s in spec.sort
        )
    version = draw(st.one_of(st.none(), st.integers(0, 50)))
    plan = QueryPlan(spec, TABLE, entities, after, version, spec.limit + 1)
    supplied = {str(v) for v in entities} | {str(v) for v in after or ()}
    for f in spec.filters:
        match f:
            case Eq(_, v):
                supplied.add(str(v))
            case In(_, vs):
                supplied |= {str(v) for v in vs}
            case Range(_, low, high):
                supplied |= {str(v) for v in (low, high) if v is not None}
            case Prefix(_, p):
                supplied.add(p)
    return plan, supplied


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return [*_conjuncts(node.left), *_conjuncts(node.right)]
    return [node]


def _check(plan: QueryPlan, supplied: set[str]) -> None:
    rendered = render(plan, IDENTIFIER)
    text = rendered.text
    for value in supplied:
        assert value not in text, f"caller value {value!r} leaked into SQL: {text}"
    markers = _MARKER.findall(text)
    assert len(markers) == len(set(markers)), "a marker is used twice"
    assert set(markers) == set(rendered.params), "markers and bindings differ"
    tree = sqlglot.parse_one(text, read="databricks")
    assert isinstance(tree, exp.Select)
    where = tree.args["where"].this
    first = _conjuncts(where)[0]
    while isinstance(first, exp.Paren):
        first = first.this
    assert isinstance(first, exp.In), f"the entity predicate is not the first top-level conjunct: {text}"
    assert first.this.name == TABLE.entity_column
    assert all(isinstance(p, exp.Placeholder) for p in first.expressions)
    assert {rendered.params[p.name] for p in first.expressions} == set(plan.entities)


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sentinel_plans())
def test_rendered_sql_binds_every_caller_value(case: tuple[QueryPlan, set[str]]) -> None:
    _check(*case)


@pytest.mark.slow
@settings(max_examples=10_000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sentinel_plans())
def test_rendered_sql_binds_every_caller_value_10k(case: tuple[QueryPlan, set[str]]) -> None:
    _check(*case)


def test_rendering_is_exactly_what_we_expect_for_a_known_plan() -> None:
    spec = QuerySpec(TABLE.key, ("row_id", "amount_minor"), (Eq("currency", "USD"),), (Sort("row_id"),), limit=5)
    plan = QueryPlan(spec, TABLE, frozenset({"US01"}), ("abc",), 3, 6)
    rendered = render(plan, IDENTIFIER)
    assert rendered.text == (
        "SELECT `row_id`, `amount_minor` FROM delta.`s3a://close-lake/local/ops/smoke_results` VERSION AS OF 3 "
        "WHERE (`entity_id` IN (:p0)) AND (`currency` = :p1) AND ((`row_id` > :p2)) ORDER BY `row_id` ASC LIMIT :p3"
    )
    assert rendered.params == {"p0": "US01", "p1": "USD", "p2": "abc", "p3": 6}
