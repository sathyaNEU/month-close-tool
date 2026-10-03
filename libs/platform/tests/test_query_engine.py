"""The query engine without Spark: entity scope, paging on a pinned version, cursor binding, limits."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from querykit import ENTITIES, TABLE, dataset, entity_sets, row_specs

from close_contracts.query import Aggregate, Eq, QueryError, QuerySpec, Row, Sort
from close_contracts.query_cursor import CursorCodec, CursorError
from close_contracts.query_reference import evaluate
from close_platform.query import Caller, QueryEngine
from close_platform.query.reference import ReferenceBackend

ROWS = dataset()
KEY = TABLE.key
ALICE = Caller("1001", frozenset({"US01"}))
BOTH = Caller("1004", frozenset(ENTITIES[:2]))


def engine(rows: list[Row] | None = None) -> tuple[QueryEngine, ReferenceBackend]:
    backend = ReferenceBackend()
    backend.load(KEY, ROWS if rows is None else rows)
    return QueryEngine(backend, CursorCodec(b"c" * 32)), backend


def spec(**kw: object) -> QuerySpec:
    base: dict[str, object] = {
        "table": KEY,
        "columns": ("row_id", "entity_id", "amount_minor"),
        "sort": (Sort("row_id"),),
    }
    return QuerySpec(**{**base, **kw})  # type: ignore[arg-type]


def test_every_query_carries_the_callers_entities_and_nothing_else_leaks() -> None:
    eng, backend = engine()
    page = eng.run(spec(limit=500), ALICE)
    assert page.rows
    assert {r["entity_id"] for r in page.rows} == {"US01"}
    assert backend.plans[-1].entities == frozenset({"US01"})


def test_no_grants_means_no_query_at_all() -> None:
    eng, backend = engine()
    page = eng.run(spec(), Caller("1003", frozenset()))
    assert (page.rows, page.next_cursor) == ((), None)
    assert backend.plans == []


def test_a_filter_on_the_entity_column_can_only_narrow() -> None:
    eng, _ = engine()
    assert eng.run(spec(filters=(Eq("entity_id", "GB01"),)), ALICE).rows == ()


def test_invalid_specs_never_reach_the_backend() -> None:
    eng, backend = engine()
    with pytest.raises(QueryError):
        eng.run(spec(sort=()), ALICE)
    with pytest.raises(QueryError, match="not a queryable table"):
        eng.run(QuerySpec("ops.writer_claims", ("x",)), ALICE)
    assert backend.plans == []


@settings(max_examples=60, deadline=None)
@given(row_specs(max_limit=25), entity_sets)
def test_paging_through_cursors_returns_the_full_result_exactly_once(s: QuerySpec, entities: frozenset[str]) -> None:
    eng, _ = engine()
    caller = Caller("u1", entities)
    collected: list[Row] = []
    page = eng.run(s, caller)
    collected.extend(page.rows)
    while page.next_cursor is not None:
        page = eng.run(s, caller, page.next_cursor)
        collected.extend(page.rows)
    assert collected == evaluate(s, TABLE, ROWS, entities=entities, fetch=len(ROWS))


def test_later_pages_read_the_version_the_first_page_saw() -> None:
    eng, backend = engine()
    first = eng.run(spec(limit=10), BOTH)
    assert first.version == 0
    assert first.next_cursor is not None
    backend.load(KEY, [{**r, "amount_minor": 0} for r in ROWS] + [{**ROWS[0], "row_id": "0" * 32}])  # version 1
    second = eng.run(spec(limit=10), BOTH, first.next_cursor)
    assert backend.plans[-1].version == 0
    assert second.version == 0
    expected = evaluate(spec(limit=10), TABLE, ROWS, entities=BOTH.entities, fetch=20)[10:20]
    assert list(second.rows) == expected
    assert eng.run(spec(limit=10), BOTH).version == 1  # a new paging session sees the new version


def test_cursors_are_bound_to_the_spec_and_the_caller() -> None:
    eng, _ = engine()
    first = eng.run(spec(limit=5), BOTH)
    assert first.next_cursor is not None
    with pytest.raises(CursorError, match="different user"):
        eng.run(spec(limit=5), Caller("1005", BOTH.entities), first.next_cursor)
    with pytest.raises(CursorError, match="different query"):
        eng.run(spec(limit=6), BOTH, first.next_cursor)


def test_a_revoked_grant_takes_effect_in_the_middle_of_paging() -> None:
    eng, _ = engine()
    first = eng.run(spec(limit=5), BOTH)
    assert first.next_cursor is not None
    later = eng.run(spec(limit=5), Caller(BOTH.user_id, frozenset({"EU01"})), first.next_cursor)
    assert {r["entity_id"] for r in later.rows} <= {"EU01"}


def test_grouped_results_are_one_page_and_never_silently_truncated() -> None:
    eng, _ = engine()
    grouped = QuerySpec(KEY, group_by=("currency", "entity_id"), aggregates=(Aggregate("count", "n"),), limit=3)
    with pytest.raises(QueryError, match="more than 3 groups"):
        eng.run(grouped, BOTH)
    ok = QuerySpec(KEY, group_by=("entity_id",), aggregates=(Aggregate("count", "n"),), limit=10)
    page = eng.run(ok, BOTH)
    assert [r["entity_id"] for r in page.rows] == sorted(BOTH.entities)
    assert page.next_cursor is None
    with pytest.raises(QueryError, match="single page"):
        eng.run(ok, BOTH, "anything")


def test_a_table_that_was_never_written_reads_as_empty() -> None:
    eng = QueryEngine(ReferenceBackend(), CursorCodec(b"c" * 32))
    page = eng.run(spec(), ALICE)
    assert (page.rows, page.version) == ((), None)
