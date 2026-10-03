"""Shared test kit for the read path: a deterministic dataset shaped like ``ops.smoke_results`` and
hypothesis strategies for valid QuerySpecs. The contracts, platform and renderer tests all draw from
here, so the three executors are held to the same generated queries."""

from __future__ import annotations

import hashlib
from typing import Final, Literal

from hypothesis import strategies as st

from close_contracts.query import Aggregate, Eq, Filter, In, Prefix, QuerySpec, Range, Row, Scalar, Sort
from close_contracts.queryable import SMOKE_RESULTS

ENTITIES = ("US01", "EU01", "GB01")  # GB01 is granted to nobody in the tests: it must never leak
RUNS = ("r" + "0" * 31, "r" + "1" * 31)
CURRENCIES = ("USD", "EUR", "GBP", "JPY")
TABLE = SMOKE_RESULTS


def dataset(n: int = 400, seed: int = 7) -> list[Row]:
    """Deterministic rows: duplicate amounts and currencies (so ties exercise the unique key), negative
    amounts, three entities, two runs, and row ids that share hex prefixes."""
    rows: list[Row] = []
    for i in range(n):
        h = hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()
        amount = (int(h[8:16], 16) % 2001) - 1000
        rows.append(
            {
                "run_id": RUNS[i % len(RUNS)],
                "seq": i,
                "row_id": h[:32],
                "entity_id": ENTITIES[int(h[16:18], 16) % len(ENTITIES)],
                "currency": CURRENCIES[int(h[18:20], 16) % len(CURRENCIES)],
                "amount_minor": amount * (10 if i % 5 == 0 else 1),
                "amount_eur_minor": amount * 9,
            }
        )
    return rows


BIGINT_COLS = ("seq", "amount_minor", "amount_eur_minor")
STRING_COLS = ("run_id", "row_id", "entity_id", "currency")
ALL_COLS = tuple(c.name for c in TABLE.columns)

# Values the dataset contains, plus sentinels it never contains (random strings in the renderer test).
_ints = st.one_of(st.integers(-1100, 1100), st.integers(-(2**63), 2**63 - 1))
NO_SURROGATES: Final[tuple[Literal["Cs"], ...]] = ("Cs",)  # they cannot be encoded as UTF-8
_texts = st.text(alphabet=st.characters(exclude_categories=NO_SURROGATES), min_size=0, max_size=12)


def _string_value(col: str) -> st.SearchStrategy[str]:
    known = {"run_id": RUNS, "entity_id": ENTITIES, "currency": CURRENCIES}.get(col, ())
    return st.one_of(st.sampled_from(known), _texts) if known else _texts


def _value(col: str) -> st.SearchStrategy[Scalar]:
    return _ints if col in BIGINT_COLS else _string_value(col)


@st.composite
def filters(draw: st.DrawFn) -> Filter:
    col = draw(st.sampled_from(ALL_COLS))
    kind = draw(st.sampled_from(("eq", "in", "range", "prefix") if col in STRING_COLS else ("eq", "in", "range")))
    if kind == "eq":
        return Eq(col, draw(_value(col)))
    if kind == "in":
        return In(col, tuple(draw(st.lists(_value(col), min_size=1, max_size=6))))
    if kind == "range":
        low = draw(st.one_of(st.none(), _value(col)))
        high = draw(_value(col)) if low is None else draw(st.one_of(st.none(), _value(col)))
        return Range(col, low, high)
    prefix = draw(st.one_of(st.text(alphabet="0123456789abcdef", min_size=1, max_size=3), _texts.filter(bool)))
    return Prefix(col, prefix)


@st.composite
def row_specs(draw: st.DrawFn, max_limit: int = 60) -> QuerySpec:
    leading = draw(st.lists(st.sampled_from([c for c in ALL_COLS if c != "row_id"]), unique=True, max_size=3))
    sort = tuple(Sort(c, draw(st.booleans())) for c in (*leading, "row_id"))
    extra = draw(st.lists(st.sampled_from(ALL_COLS), unique=True, max_size=3))
    columns = tuple(dict.fromkeys([*(s.column for s in sort), *extra]))
    return QuerySpec(
        table=TABLE.key,
        columns=columns,
        filters=tuple(draw(st.lists(filters(), max_size=3))),
        sort=sort,
        limit=draw(st.integers(1, max_limit)),
    )


@st.composite
def grouped_specs(draw: st.DrawFn) -> QuerySpec:
    group_by = tuple(draw(st.lists(st.sampled_from(("run_id", "entity_id", "currency")), unique=True, max_size=2)))
    aggregates = [Aggregate("count", "n")]
    if draw(st.booleans()):
        aggregates.append(Aggregate("sum", "total", draw(st.sampled_from(BIGINT_COLS))))
    if draw(st.booleans()):
        aggregates.append(Aggregate("count", "n_col", draw(st.sampled_from(ALL_COLS))))
    return QuerySpec(
        table=TABLE.key,
        filters=tuple(draw(st.lists(filters(), max_size=2))),
        group_by=group_by,
        aggregates=tuple(aggregates),
        limit=draw(st.integers(1, 1000)),
    )


def specs() -> st.SearchStrategy[QuerySpec]:
    return st.one_of(row_specs(), grouped_specs())


entity_sets = st.frozensets(st.sampled_from(ENTITIES[:2]), max_size=2)
