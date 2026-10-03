"""QuerySpec validation, the signed cursor and the reference evaluator's semantics (Stage 1b)."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from querykit import ENTITIES, NO_SURROGATES, TABLE, dataset, entity_sets, row_specs, specs

from close_contracts.query import (
    Aggregate,
    Column,
    Eq,
    In,
    Prefix,
    QueryableTable,
    QueryError,
    QuerySpec,
    Range,
    Row,
    Sort,
    typed,
    untyped,
    validate,
)
from close_contracts.query_cursor import CursorCodec, CursorError
from close_contracts.query_reference import evaluate, sort_key
from close_contracts.queryable import QUERYABLE, RUN_MANIFESTS, queryable
from close_contracts.tables import Tables

KEY = TABLE.key
ROWS = dataset()
BOTH = frozenset(ENTITIES[:2])


def spec(**kw: object) -> QuerySpec:
    base: dict[str, object] = {"table": KEY, "columns": ("row_id", "amount_minor"), "sort": (Sort("row_id"),)}
    return QuerySpec(**{**base, **kw})  # type: ignore[arg-type]


# ---------------------------------------------------------------- the registry


def test_registry_holds_only_the_allowlisted_tables() -> None:
    assert set(QUERYABLE) == {
        "ops.smoke_results",
        "ops.run_manifests",
        "ledger.journal_lines",
        "ledger.journal_postings",
        "ledger.gl_rollup_lines",
    }
    assert queryable("ops.run_manifests") is RUN_MANIFESTS
    with pytest.raises(QueryError, match="not a queryable table"):
        queryable("ops.writer_claims")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"entity_column": "seq"}, "entity column"),
        ({"entity_column": "nope"}, "entity column"),
        ({"unique_key": ()}, "unique key is required"),
        ({"unique_key": ("maybe",)}, "unique key column"),
        ({"columns": (Column("a", "string"), Column("a", "string"))}, "duplicate"),
    ],
)
def test_bad_table_definitions_are_rejected(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "table": Tables.OPS_SMOKE_RESULTS,
        "columns": (Column("entity_id", "string"), Column("seq", "bigint"), Column("maybe", "string", nullable=True)),
        "entity_column": "entity_id",
        "unique_key": ("seq",),
    }
    with pytest.raises(ValueError, match=message):
        QueryableTable(**{**base, **kwargs})  # type: ignore[arg-type]


def test_column_names_are_checked() -> None:
    with pytest.raises(ValueError, match="invalid column name"):
        Column("Drop Table", "string")


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (spec(table="ops.run_manifests"), "not 'ops.smoke_results'"),
        (spec(limit=0), "positive integer"),
        (spec(limit=True), "positive integer"),
        (spec(limit=501), "at most 500"),
        (spec(columns=()), "distinct columns"),
        (spec(columns=("row_id", "row_id")), "distinct columns"),
        (spec(columns=("row_id", "nope")), "no column 'nope'"),
        (spec(sort=()), "end with the unique key"),
        (spec(sort=(Sort("row_id"), Sort("seq"))), "end with the unique key"),
        (spec(sort=(Sort("seq"), Sort("row_id"))), "must also be a selected column"),
        (spec(sort=(Sort("row_id"), Sort("row_id"))), "only once"),
        (spec(group_by=("currency",)), "at least one aggregate"),
        (spec(filters=(Eq("seq", "7"),)), "64-bit integer"),
        (spec(filters=(Eq("seq", True),)), "64-bit integer"),
        (spec(filters=(Eq("seq", 2**63),)), "64-bit integer"),
        (spec(filters=(Eq("currency", 7),)), "needs a string"),
        (spec(filters=(Eq("currency", "x" * 1025),)), "limited to 1024"),
        (spec(filters=(Eq("currency", "\ud800"),)), "not valid Unicode"),
        (spec(filters=(In("currency", ()),)), "1 to 200"),
        (spec(filters=(In("currency", tuple("x" * 201)),)), "1 to 200"),
        (spec(filters=(Range("seq"),)), "low or a high"),
        (spec(filters=(Range("seq", "1"),)), "64-bit integer"),
        (spec(filters=(Prefix("seq", "1"),)), "not a string column"),
        (spec(filters=(Prefix("currency", ""),)), "non-empty prefix"),
        (spec(filters=(Eq("nope", 1),)), "no column 'nope'"),
    ],
)
def test_invalid_row_specs_are_rejected(bad: QuerySpec, message: str) -> None:
    with pytest.raises(QueryError, match=message):
        validate(bad, TABLE)


def grouped(**kw: object) -> QuerySpec:
    base: dict[str, object] = {"table": KEY, "group_by": ("currency",), "aggregates": (Aggregate("count", "n"),)}
    return QuerySpec(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (grouped(columns=("currency",)), "no columns or sort"),
        (grouped(sort=(Sort("currency"),)), "no columns or sort"),
        (grouped(group_by=("currency", "currency")), "distinct"),
        (grouped(limit=1001), "at most 1000"),
        (grouped(aggregates=(Aggregate("count", "n"), Aggregate("count", "n"))), "distinct"),
        (grouped(aggregates=(Aggregate("count", "currency"),)), "differ from the group columns"),
        (grouped(aggregates=(Aggregate("count", "Bad Name"),)), "invalid aggregate name"),
        (grouped(aggregates=(Aggregate("sum", "t"),)), "needs a bigint column"),
        (grouped(aggregates=(Aggregate("sum", "t", "currency"),)), "needs a bigint column"),
        (grouped(aggregates=(Aggregate("count", "n", "nope"),)), "no column 'nope'"),
        (grouped(aggregates=(Aggregate("max", "m", "seq"),)), "unknown aggregate"),  # type: ignore[arg-type]
    ],
)
def test_invalid_grouped_specs_are_rejected(bad: QuerySpec, message: str) -> None:
    with pytest.raises(QueryError, match=message):
        validate(bad, TABLE)


def test_nullable_columns_cannot_order_or_group() -> None:
    t = QueryableTable(
        table=Tables.OPS_SMOKE_RESULTS,
        columns=(Column("entity_id", "string"), Column("id", "bigint"), Column("note", "string", nullable=True)),
        entity_column="entity_id",
        unique_key=("id",),
    )
    with pytest.raises(QueryError, match="nullable"):
        validate(QuerySpec(KEY, ("note", "id"), sort=(Sort("note"), Sort("id"))), t)
    with pytest.raises(QueryError, match="nullable"):
        validate(QuerySpec(KEY, group_by=("note",), aggregates=(Aggregate("count", "n"),)), t)


@given(specs())
def test_generated_specs_are_valid(s: QuerySpec) -> None:
    validate(s, TABLE)


# ---------------------------------------------------------------- fingerprints and typed values


def test_fingerprint_is_stable_and_sensitive() -> None:
    a = spec(filters=(Eq("seq", 2**62),))
    assert a.fingerprint() == spec(filters=(Eq("seq", 2**62),)).fingerprint()
    assert a.fingerprint() != spec(filters=(Eq("seq", 2**62 + 1),)).fingerprint()
    assert a.fingerprint() != spec(filters=(Eq("seq", 2**62),), limit=99).fingerprint()
    assert spec(filters=(Eq("currency", "1"),)).fingerprint() != spec(filters=(In("currency", ("1",)),)).fingerprint()


@given(st.one_of(st.integers(-(2**63), 2**63 - 1), st.text(alphabet=st.characters(exclude_categories=NO_SURROGATES))))
def test_typed_values_round_trip(value: int | str) -> None:
    assert untyped(typed(value)) == value


def test_typed_rejects_other_types() -> None:
    with pytest.raises(QueryError):
        typed(1.5)  # type: ignore[arg-type]
    with pytest.raises(QueryError):
        typed(True)
    with pytest.raises(QueryError):
        untyped(["x", "1"])
    assert typed(None) is None
    assert untyped(None) is None


# ---------------------------------------------------------------- cursors

KEY_BYTES = b"k" * 32


def test_cursor_round_trip_with_big_integers() -> None:
    codec = CursorCodec(KEY_BYTES)
    token = codec.issue(spec="abc", principal="1001", version=7, after=(2**62, "ffee"))
    state = codec.open(token, spec="abc", principal="1001")
    assert (state.version, state.after) == (7, (2**62, "ffee"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"), "invalid cursor"),
        (lambda t: "x" + t, "invalid cursor|malformed"),
        (lambda t: t.replace(".", ""), "malformed"),
        (lambda _: "not-base64!.also-not!", "malformed|invalid"),
    ],
)
def test_tampered_cursors_are_rejected(mutate: object, message: str) -> None:
    codec = CursorCodec(KEY_BYTES)
    token = codec.issue(spec="abc", principal="1001", version=1, after=("a",))
    with pytest.raises(CursorError, match=message):
        codec.open(mutate(token), spec="abc", principal="1001")  # type: ignore[operator]


def test_cursors_are_bound_to_the_spec_the_user_and_the_clock() -> None:
    now = [1_000_000.0]
    codec = CursorCodec(KEY_BYTES, ttl_s=60, clock=lambda: now[0])
    token = codec.issue(spec="abc", principal="1001", version=None, after=(1,))
    with pytest.raises(CursorError, match="different query"):
        codec.open(token, spec="abd", principal="1001")
    with pytest.raises(CursorError, match="different user"):
        codec.open(token, spec="abc", principal="1002")
    now[0] += 61
    with pytest.raises(CursorError, match="expired"):
        codec.open(token, spec="abc", principal="1001")
    with pytest.raises(CursorError, match="invalid"):
        CursorCodec(b"z" * 32).open(token, spec="abc", principal="1001")
    with pytest.raises(ValueError, match="32 bytes"):
        CursorCodec(b"short")


# ---------------------------------------------------------------- reference semantics


def test_entity_scope_is_applied_before_everything() -> None:
    out = evaluate(spec(columns=("row_id", "entity_id"), limit=500), TABLE, ROWS, entities=frozenset({"US01"}))
    assert out
    assert {r["entity_id"] for r in out} == {"US01"}
    assert evaluate(spec(), TABLE, ROWS, entities=frozenset()) == []


def test_filters_follow_the_documented_semantics() -> None:
    rows: list[Row] = [
        {"row_id": "a1", "entity_id": "US01", "currency": "USD", "seq": 1, "amount_minor": -5},
        {"row_id": "a2", "entity_id": "US01", "currency": None, "seq": 2, "amount_minor": 0},
        {"row_id": "b1", "entity_id": "US01", "currency": "EUR", "seq": 3, "amount_minor": 5},
    ]
    us = frozenset({"US01"})

    def ids(*filters: object) -> list[object]:
        return [r["row_id"] for r in evaluate(spec(filters=filters), TABLE, rows, entities=us)]

    assert ids(Range("amount_minor", -5, 5)) == ["a1", "a2"]  # half-open: 5 is excluded
    assert ids(Range("amount_minor", 0)) == ["a2", "b1"]
    assert ids(Range("amount_minor", None, 0)) == ["a1"]
    assert ids(Prefix("row_id", "a")) == ["a1", "a2"]
    assert ids(In("currency", ("USD", "EUR"))) == ["a1", "b1"]  # NULL never matches
    assert ids(Eq("currency", "USD"), Eq("seq", 1)) == ["a1"]
    assert ids(Range("amount_minor", 9, 1)) == []  # an empty range is simply empty


def test_mixed_sort_directions_and_ties_use_the_unique_key() -> None:
    s = spec(
        columns=("currency", "amount_minor", "row_id"),
        sort=(Sort("currency", True), Sort("amount_minor"), Sort("row_id", True)),
        limit=500,
    )
    out = evaluate(s, TABLE, ROWS, entities=BOTH)
    keys = [sort_key(r, s.sort) for r in out]
    for (c1, a1, r1), (c2, a2, r2) in itertools.pairwise(keys):
        assert c1 > c2 or (c1 == c2 and (a1 < a2 or (a1 == a2 and r1 > r2)))  # type: ignore[operator]


def test_grouped_counts_and_sums() -> None:
    s = grouped(
        group_by=("entity_id",), aggregates=(Aggregate("count", "n"), Aggregate("sum", "total", "amount_minor"))
    )
    out = evaluate(s, TABLE, ROWS, entities=BOTH)
    assert [r["entity_id"] for r in out] == sorted(ENTITIES[:2])
    for r in out:
        members = [x for x in ROWS if x["entity_id"] == r["entity_id"]]
        assert r["n"] == len(members)
        assert r["total"] == sum(int(str(m["amount_minor"])) for m in members)


def test_a_global_aggregate_over_nothing_is_one_row() -> None:
    s = QuerySpec(KEY, aggregates=(Aggregate("count", "n"), Aggregate("sum", "t", "seq")), filters=(Eq("seq", -1),))
    assert evaluate(s, TABLE, ROWS, entities=BOTH) == [{"n": 0, "t": None}]


def test_sum_overflow_is_an_error_like_ansi_spark() -> None:
    rows: list[Row] = [{"row_id": str(i), "entity_id": "US01", "seq": 2**62} for i in range(3)]
    s = QuerySpec(KEY, aggregates=(Aggregate("sum", "t", "seq"),))
    with pytest.raises(QueryError, match="overflows"):
        evaluate(s, TABLE, rows, entities=frozenset({"US01"}))


@settings(max_examples=150, deadline=None)
@given(row_specs(max_limit=40), entity_sets)
def test_keyset_paging_returns_the_full_result_exactly_once(s: QuerySpec, entities: frozenset[str]) -> None:
    everything = evaluate(s, TABLE, ROWS, entities=entities, fetch=len(ROWS))
    pages: list[Row] = []
    after = None
    while True:
        page = evaluate(s, TABLE, ROWS, entities=entities, after=after, fetch=s.limit + 1)
        pages.extend(page[: s.limit])
        if len(page) <= s.limit:
            break
        after = sort_key(page[s.limit - 1], s.sort)
    assert pages == everything
