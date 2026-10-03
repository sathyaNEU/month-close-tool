"""Interpreter vectors: the Spark (ANSI) semantics every function must follow, null handling, and the
input errors that reject a row. Stage 3's compiler is held to the same vectors on Spark."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from close_contracts.calendar import BusinessCalendar
from close_contracts.dsl import DslEvalError, DslType, evaluate, parse, typecheck
from close_contracts.dsl.functions import FUNCTIONS
from close_contracts.dsl.interpret import _LAZY, _STRICT

T = DslType


def col(name: str) -> dict[str, Any]:
    return {"col": name}


def lit(value: Any, type_: str) -> dict[str, Any]:
    return {"lit": value, "type": type_}


def fn(name: str, *args: dict[str, Any]) -> dict[str, Any]:
    return {"fn": name, "args": list(args)}


def s(v: str | None) -> dict[str, Any]:
    return lit(v, "string")


def n(v: int | None) -> dict[str, Any]:
    return lit(v, "bigint")


def run(expr: dict[str, Any], row: dict[str, Any] | None = None, schema: dict[str, DslType] | None = None) -> Any:
    ir = typecheck(parse(expr), schema or {})
    cal = BusinessCalendar.of("US_FED", holidays=[date(2026, 10, 12)])
    return evaluate(ir, row or {}, calendars={"US_FED": cal})


VECTORS: list[tuple[dict[str, Any], Any]] = [
    (fn("trim", s("  a b  ")), "a b"),
    (fn("trim", s("\ta\t")), "\ta\t"),  # Spark trims spaces only
    (fn("normalize_text", s("Ref: ORD-0042/x")), "reford0042x"),
    (fn("substring", s("abcdef"), n(2), n(3)), "bcd"),
    (fn("substring", s("abcdef"), n(0), n(2)), "ab"),  # position 0 behaves like 1
    (fn("substring", s("abcdef"), n(-2), n(5)), "ef"),
    (fn("substring", s("abc"), n(2), n(-1)), ""),
    (fn("regex_extract", s("pay TX12345678XT"), s(r"TX(\d{8})XT"), n(1)), "12345678"),
    (fn("regex_extract", s("no match"), s(r"TX(\d{8})XT"), n(1)), ""),  # Spark: empty, not null
    (fn("regex_extract", s("ab"), s(r"a(x)?b"), n(1)), ""),  # an unused group is empty
    (fn("regex_matches", s("xxTRN*1*ABC"), s(r"TRN\*1\*")), True),
    (fn("split_part", s("a,b,c"), s(","), n(2)), "b"),
    (fn("split_part", s("a,b"), s(","), n(5)), ""),
    (fn("concat", s("a"), s("b"), s("c")), "abc"),
    (fn("contains", s("abc"), s("")), True),
    (fn("cast_string", lit("1.5", "decimal")), "1.500000000000"),
    (fn("cast_string", lit(True, "boolean")), "true"),
    (fn("cast_string", lit("2026-01-02", "date")), "2026-01-02"),
    (fn("abs_diff", n(-5), n(7)), 12),
    (fn("bps_of", n(-10_001), n(25)), 25),  # floor(10,001 x 25 / 10,000)
    (fn("parse_bigint", s("-42")), -42),
    (fn("to_minor_units", fn("parse_implied_decimals", s("123456"), n(2)), s("USD")), 123456),
    (fn("to_minor_units", fn("parse_decimal_comma", s("1234,5")), s("EUR")), 123450),
    (fn("to_minor_units", fn("parse_decimal_comma", s("100,")), s("GBP")), 10000),
    (fn("to_minor_units", fn("parse_decimal", s("-0.07")), s("USD")), -7),
    (fn("to_minor_units", fn("parse_decimal", s("125")), s("JPY")), 125),
    (fn("currency_exponent", s("JPY")), 0),
    (fn("to_date", s("990131"), s("yyMMdd")), date(2099, 1, 31)),  # Spark: yy is 2000-2099
    (fn("to_date", s("30.09.2026"), s("dd.MM.yyyy")), date(2026, 9, 30)),
    (
        fn("to_timestamp", s("2026-09-30T23:59:59Z"), s("yyyy-MM-dd'T'HH:mm:ss'Z'")),
        datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC),
    ),
    (fn("date_of", lit("2026-09-30T23:59:59Z", "timestamp")), date(2026, 9, 30)),
    (fn("date_add_days", lit("2026-02-27", "date"), n(2)), date(2026, 3, 1)),
    (fn("days_between", lit("2026-09-30", "date"), lit("2026-10-02", "date")), 2),
    (fn("business_days_between", lit("2026-10-09", "date"), lit("2026-10-13", "date"), s("US_FED")), 1),
    (fn("if", lit(None, "boolean"), n(1), n(2)), 2),  # CASE WHEN null -> else
    (fn("coalesce", n(None), n(None), n(3)), 3),
]

NULLS: list[tuple[dict[str, Any], Any]] = [
    (fn("and", lit(None, "boolean"), lit(False, "boolean")), False),
    (fn("and", lit(None, "boolean"), lit(True, "boolean")), None),
    (fn("or", lit(None, "boolean"), lit(True, "boolean")), True),
    (fn("or", lit(None, "boolean"), lit(False, "boolean")), None),
    (fn("not", lit(None, "boolean")), None),
    (fn("is_null", s(None)), True),
    (fn("is_not_null", s(None)), False),
    (fn("eq", s(None), s(None)), None),
    (fn("lower", s(None)), None),
    (fn("concat", s("a"), s(None)), None),  # Spark concat is strict
    (fn("add", n(1), n(None)), None),
]

ERRORS: list[dict[str, Any]] = [
    fn("parse_bigint", s("12a")),
    fn("parse_decimal", s("1,5")),
    fn("parse_decimal_comma", s("1.5")),
    fn("to_minor_units", fn("parse_decimal", s("1.005")), s("USD")),  # more decimals than USD allows
    fn("to_minor_units", fn("parse_decimal", s("1")), s("XXQ")),
    fn("to_date", s("2026-02-30"), s("yyyy-MM-dd")),
    fn("to_date", s("2026-9-3"), s("yyyy-MM-dd")),  # fixed-width fields, as Spark requires
    fn("bps_of", n(100), n(-1)),
    fn("business_days_between", lit("2026-10-09", "date"), lit("2026-10-13", "date"), s("TARGET2")),
]

OVERFLOW: list[tuple[dict[str, Any], int]] = [
    (fn("add", col("x"), n(1)), 2**63 - 1),
    (fn("sub", col("x"), n(1)), -(2**63)),
    (fn("neg", col("x")), -(2**63)),
    (fn("abs", col("x")), -(2**63)),
    (fn("mul", col("x"), n(4)), 2**62),
]


@pytest.mark.parametrize(("expr", "expected"), VECTORS)
def test_functions_follow_spark_semantics(expr: dict[str, Any], expected: Any) -> None:
    result = run(expr)
    assert result == expected, (result, expected)
    assert type(result) is type(expected), (result, expected)


@pytest.mark.parametrize(("expr", "expected"), NULLS)
def test_null_handling_follows_sql(expr: dict[str, Any], expected: Any) -> None:
    assert run(expr) is expected


@pytest.mark.parametrize("expr", ERRORS)
def test_bad_input_rejects_the_row(expr: dict[str, Any]) -> None:
    with pytest.raises(DslEvalError):
        run(expr)


def test_every_catalog_function_has_an_implementation() -> None:
    implemented = set(_LAZY) | set(_STRICT) | {"business_days_between"}
    assert implemented == set(FUNCTIONS)


def test_every_catalog_function_is_covered_by_a_vector() -> None:
    def names(e: dict[str, Any]) -> set[str]:
        return ({e["fn"]} | set().union(*(names(a) for a in e["args"]))) if "fn" in e else set()

    covered = set().union(*(names(e) for e, _ in VECTORS + NULLS), *(names(e) for e in ERRORS))
    covered |= set().union(*(names(e) for e, _ in OVERFLOW))
    others = {"lt", "le", "gt", "ge", "ne", "upper", "length", "starts_with", "ends_with", "sub", "abs"}
    assert set(FUNCTIONS) - covered <= others


def test_rows_supply_column_values() -> None:
    schema = {"a.amount_minor": T.BIGINT, "b.amount_minor": T.BIGINT}
    expr = fn("le", fn("abs_diff", col("a.amount_minor"), col("b.amount_minor")), n(5))
    assert run(expr, {"a.amount_minor": 1000, "b.amount_minor": 1004}, schema) is True
    assert run(expr, {"a.amount_minor": 1000, "b.amount_minor": 1006}, schema) is False
    assert run(fn("gt", lit("1.5", "decimal"), lit("1.25", "decimal"))) is True
    assert run(fn("eq", lit("2026-01-01", "date"), lit("2026-01-01", "date"))) is True
    assert isinstance(run(fn("parse_decimal", s("1.10"))), Decimal)


@pytest.mark.parametrize(("expr", "value"), OVERFLOW)
def test_integer_overflow_is_an_error_as_in_ansi_mode(expr: dict[str, Any], value: int) -> None:
    # Literals are limited to the JSON-safe range, so the large operands arrive as column values.
    with pytest.raises(DslEvalError, match="overflow"):
        run(expr, {"x": value}, {"x": T.BIGINT})
