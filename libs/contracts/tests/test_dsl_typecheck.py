"""The DSL typecheck corpus: valid expressions type to the expected result, invalid ones fail with a
stable code at the right path, and the limits hold."""

from __future__ import annotations

from typing import Any

import pytest

from close_contracts.dsl import DslType, DslTypeError, IrCall, parse, typecheck
from close_contracts.dsl.ir import columns, fingerprint
from close_contracts.dsl.typecheck import MAX_DEPTH, MAX_NODES

T = DslType
SCHEMA = {
    "a.order_id": T.STRING,
    "b.ref": T.STRING,
    "a.amount_minor": T.BIGINT,
    "b.amount_minor": T.BIGINT,
    "a.value_date": T.DATE,
    "b.value_date": T.DATE,
    "a.captured_at": T.TIMESTAMP,
    "b.amount_text": T.STRING,
    "b.currency": T.STRING,
    "a.is_refund": T.BOOLEAN,
    "b.fx_rate": T.DECIMAL,
}


def col(name: str) -> dict[str, Any]:
    return {"col": name}


def lit(value: Any, type_: str) -> dict[str, Any]:
    return {"lit": value, "type": type_}


def fn(name: str, *args: dict[str, Any]) -> dict[str, Any]:
    return {"fn": name, "args": list(args)}


VALID: list[tuple[dict[str, Any], DslType]] = [
    (col("a.order_id"), T.STRING),
    (lit("2026-09-30", "date"), T.DATE),
    (lit("2026-09-30T23:59:59Z", "timestamp"), T.TIMESTAMP),
    (lit("0.012500", "decimal"), T.DECIMAL),
    (lit(None, "bigint"), T.BIGINT),
    (fn("eq", col("a.order_id"), fn("upper", col("b.ref"))), T.BOOLEAN),
    (fn("le", fn("abs_diff", col("a.amount_minor"), col("b.amount_minor")), lit(5, "bigint")), T.BOOLEAN),
    (fn("regex_extract", col("b.ref"), lit(r"ORD-(\d+)", "string"), lit(1, "bigint")), T.STRING),
    (
        fn("to_minor_units", fn("parse_implied_decimals", col("b.amount_text"), lit(2, "bigint")), col("b.currency")),
        T.BIGINT,
    ),
    (fn("to_date", col("b.ref"), lit("yyMMdd", "string")), T.DATE),
    (fn("business_days_between", col("a.value_date"), col("b.value_date"), lit("US_FED", "string")), T.BIGINT),
    (fn("coalesce", col("a.order_id"), col("b.ref"), lit("", "string")), T.STRING),
    (fn("if", col("a.is_refund"), fn("neg", col("a.amount_minor")), col("a.amount_minor")), T.BIGINT),
    (fn("and", fn("is_not_null", col("b.ref")), fn("not", col("a.is_refund"))), T.BOOLEAN),
    (fn("concat", col("a.order_id"), lit("-", "string"), col("b.ref")), T.STRING),
    (fn("date_of", col("a.captured_at")), T.DATE),
    (fn("gt", col("b.fx_rate"), lit("1.5", "decimal")), T.BOOLEAN),
    (fn("cast_string", col("b.fx_rate")), T.STRING),
]

INVALID: list[tuple[dict[str, Any], str, str]] = [
    (col("a.missing"), "unknown_column", ""),
    (fn("frobnicate", col("a.order_id")), "unknown_function", ""),
    (fn("lower", col("a.amount_minor")), "type_mismatch", ""),
    (fn("eq", col("a.order_id"), col("a.amount_minor")), "type_mismatch", ""),
    (fn("lower"), "arity", ""),
    (fn("not", col("a.is_refund"), col("a.is_refund")), "arity", ""),
    (fn("and", col("a.is_refund")), "type_mismatch", ""),
    (fn("lt", col("a.is_refund"), col("a.is_refund")), "type_mismatch", ""),
    (fn("regex_extract", col("b.ref"), col("b.ref"), lit(1, "bigint")), "not_literal", "/args/1"),
    (fn("regex_extract", col("b.ref"), lit("(a+)+", "string"), lit(1, "bigint")), "regex_nested_quantifier", "/args/1"),
    (fn("regex_extract", col("b.ref"), lit("x(y)", "string"), lit(2, "bigint")), "bad_literal", "/args/2"),
    (fn("regex_matches", col("b.ref"), lit("x$", "string")), "regex_end_anchor", "/args/1"),
    (fn("to_date", col("b.ref"), lit("yyyy/dd/MM", "string")), "bad_literal", "/args/1"),
    (fn("to_date", col("b.ref"), lit(None, "string")), "not_literal", "/args/1"),
    (fn("parse_implied_decimals", col("b.ref"), lit(13, "bigint")), "bad_literal", "/args/1"),
    (fn("split_part", col("b.ref"), lit("", "string"), lit(1, "bigint")), "bad_literal", "/args"),
    (
        fn("business_days_between", col("a.value_date"), col("b.value_date"), lit("us-fed", "string")),
        "bad_literal",
        "/args/2",
    ),
    (lit("2026-02-30", "date"), "bad_literal", ""),
    (lit("30/09/2026", "date"), "bad_literal", ""),
    (lit("1.0000000000001", "decimal"), "bad_literal", ""),
    (lit("2026-09-30T24:00:00Z", "timestamp"), "bad_literal", ""),
    (lit(1, "string"), "bad_literal", ""),
    (fn("eq", col("a.order_id"), fn("lower", col("a.nope"))), "unknown_column", "/args/1/args/0"),
]


@pytest.mark.parametrize(("expr", "expected"), VALID)
def test_valid_expressions_type_to_the_expected_result(expr: dict[str, Any], expected: DslType) -> None:
    assert typecheck(parse(expr), SCHEMA).type == expected


@pytest.mark.parametrize(("expr", "code", "path"), INVALID)
def test_invalid_expressions_fail_with_a_stable_code_and_path(expr: dict[str, Any], code: str, path: str) -> None:
    with pytest.raises(DslTypeError) as info:
        typecheck(parse(expr), SCHEMA)
    assert (info.value.issues[0].code, info.value.issues[0].path) == (code, path), info.value.issues


def test_every_issue_is_reported_at_once() -> None:
    expr = fn("and", fn("eq", col("a.x"), lit(1, "bigint")), fn("lower", col("a.amount_minor")), fn("nope"))
    with pytest.raises(DslTypeError) as info:
        typecheck(parse(expr), SCHEMA)
    assert [(i.code, i.path) for i in info.value.issues] == [
        ("unknown_column", "/args/0/args/0"),
        ("type_mismatch", "/args/1"),
        ("unknown_function", "/args/2"),
    ]


def test_expect_checks_the_result_type() -> None:
    with pytest.raises(DslTypeError) as info:
        typecheck(parse(col("a.order_id")), SCHEMA, expect=T.BOOLEAN)
    assert info.value.issues[0].code == "expected_type"


def test_depth_and_size_limits() -> None:
    deep: dict[str, Any] = col("a.order_id")
    for _ in range(MAX_DEPTH):
        deep = fn("lower", deep)
    with pytest.raises(DslTypeError) as info:
        typecheck(parse(deep), SCHEMA)
    assert info.value.issues[0].code == "too_deep"
    wide = fn("concat", *[col("a.order_id")] * 60)
    big = fn("concat", *[wide] * (MAX_NODES // 60 + 1))
    with pytest.raises(DslTypeError) as info:
        typecheck(parse(big), SCHEMA)
    assert info.value.issues[0].code == "too_many_nodes"


def test_nested_and_or_are_flattened_and_the_fingerprint_is_stable() -> None:
    a, b, c = fn("is_null", col("b.ref")), col("a.is_refund"), fn("not", col("a.is_refund"))
    nested = typecheck(parse(fn("and", a, fn("and", b, c))), SCHEMA)
    flat = typecheck(parse(fn("and", a, b, c)), SCHEMA)
    assert isinstance(nested, IrCall)
    assert len(nested.args) == 3
    assert fingerprint(nested) == fingerprint(flat)
    assert columns(nested) == {"b.ref", "a.is_refund"}


@pytest.mark.parametrize(
    "literal",
    [
        lit(True, "boolean"),
        lit(-42, "bigint"),
        lit("x", "string"),
        lit("2026-09-30", "date"),
        lit("2026-09-30T23:59:59Z", "timestamp"),
        lit("-1.250000", "decimal"),
        lit(None, "date"),
    ],
)
def test_literals_return_to_their_json_form(literal: dict[str, Any]) -> None:
    from close_contracts.dsl.ir import to_json

    assert to_json(typecheck(parse(literal), SCHEMA)) == literal


def test_the_ir_json_form_carries_types_and_nesting() -> None:
    from close_contracts.dsl.ir import IrLit, literal_json, to_json

    ir = typecheck(parse(fn("add", col("a.amount_minor"), lit(1, "bigint"))), SCHEMA)
    assert to_json(ir) == {
        "fn": "add",
        "args": [{"col": "a.amount_minor", "type": "bigint"}, {"lit": 1, "type": "bigint"}],
        "type": "bigint",
    }
    assert columns(IrLit(1, T.BIGINT)) == frozenset()
    with pytest.raises(TypeError):
        literal_json("not a date", T.DATE)
