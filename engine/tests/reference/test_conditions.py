"""Pass-rule conditions: lowering to the DSL, keys, amounts, signs, dates and fuzzy text."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from close_contracts.dsl import DslTypeError
from close_datagen.world import CALENDARS
from close_engine.reference.conditions import (
    compile_rule,
    left_key,
    pair_ok,
    right_key,
    similarity_bps,
    totals_ok,
    values,
    view,
)

from .kit import DAY, amount, col, compiled_rule, fn, key, s


def _pair(conditions: list[Any], a: dict[str, Any], b: dict[str, Any], kind: str = "1:1") -> bool:
    c = compile_rule(compiled_rule(kind, [key(), *conditions]))
    row = view("a", {"reference": "R1"} | a) | view("b", {"reference": "R1"} | b)
    return pair_ok(c, row, CALENDARS)


def _text(op: str, left: str | None, right: str | None, **extra: Any) -> bool:
    condition = {"condition": "text", "op": op, "left": col("a.description"), "right": col("b.description"), **extra}
    return _pair([condition], {"description": left}, {"description": right})


@pytest.mark.parametrize(
    ("op", "left", "right", "extra", "ok"),
    [
        ("equals", "INV-1", "INV-1", {}, True),
        ("equals", "INV-1", "inv-1", {}, False),
        ("normalized_equals", "INV-1", "inv 1", {}, True),
        ("normalized_equals", "--", "  ", {}, False),  # nothing left to compare is never equal
        ("contains", "STRIPE PAYOUT TRC123", "TRC123", {}, True),
        ("contains", "STRIPE PAYOUT TRC123", "TRC", {}, False),  # a needle under 4 characters never matches
        ("contains", "STRIPE PAYOUT TRC123", "TRC", {"min_needle_length": 3}, True),
        ("starts_with", "TRC123", "TRC1", {}, True),
        ("ends_with", "TRC123", "C123", {}, True),
        ("regex_extract_equals", "ref INV-77 x", "INV-77", {"pattern": "INV-([0-9]+)"}, True),
        ("regex_extract_equals", "no ref", "none", {"pattern": "INV-([0-9]+)"}, False),  # empty extracts differ
        ("fuzzy", "ACME Holdings", "ACME Holdngs", {"algorithm": "levenshtein", "min_similarity_bps": 9000}, True),
        ("fuzzy", "ACME Holdings", "Zenith", {"algorithm": "jaro_winkler", "min_similarity_bps": 8000}, False),
        ("equals", None, "x", {}, False),
        ("fuzzy", None, "x", {"algorithm": "levenshtein", "min_similarity_bps": 0}, False),
    ],
)
def test_text_operators(op: str, left: str | None, right: str | None, extra: dict[str, Any], ok: bool) -> None:
    assert _text(op, left, right, **extra) is ok


def test_similarity_is_quantized_to_basis_points() -> None:
    assert similarity_bps("levenshtein", "kitten", "sitting") == 5714  # 3 edits over 7 characters
    assert similarity_bps("jaro_winkler", "MARTHA", "MARHTA") == 9611
    assert similarity_bps("levenshtein", "", "") == 10_000


@pytest.mark.parametrize(
    ("unit", "offset_days", "ok"),
    [("calendar_days", -1, True), ("calendar_days", 3, False), ("calendar_days", 2, True)],
)
def test_calendar_day_windows_are_asymmetric(unit: str, offset_days: int, ok: bool) -> None:
    window = {
        "condition": "date_window",
        "left": col("a.transaction_date"),
        "right": col("b.transaction_date"),
        "unit": unit,
        "before": 1,
        "after": 2,
    }
    assert _pair([window], {"transaction_date": DAY}, {"transaction_date": DAY + timedelta(days=offset_days)}) is ok


def test_business_day_windows_skip_weekends_and_holidays() -> None:
    window = {
        "condition": "date_window",
        "left": col("a.transaction_date"),
        "right": col("b.transaction_date"),
        "unit": "business_days",
        "before": 0,
        "after": 1,
        "calendar": "US_FED",
    }
    thursday = DAY + timedelta(days=1)  # 2026-07-02; Friday 3 July is a US holiday, then the weekend
    monday = DAY + timedelta(days=5)
    tuesday = monday + timedelta(days=1)
    assert _pair([window], {"transaction_date": thursday}, {"transaction_date": monday})
    assert not _pair([window], {"transaction_date": thursday}, {"transaction_date": tuesday})


def test_equal_currency_and_expressions_are_pairwise() -> None:
    currency = {"condition": "currency", "mode": "equal"}
    assert _pair([currency], {"currency": "EUR"}, {"currency": "EUR"})
    assert not _pair([currency], {"currency": "EUR"}, {"currency": "GBP"})
    expression = {"condition": "expression", "expr": fn("eq", col("a.text_1"), s("COMPLETED"))}
    assert _pair([expression], {"text_1": "COMPLETED"}, {})
    assert not _pair([expression], {"text_1": "RETURNED"}, {})


def test_fx_converted_currency_is_not_in_the_reference_yet() -> None:
    with pytest.raises(NotImplementedError):
        compile_rule(
            compiled_rule("1:1", [key(), {"condition": "currency", "mode": "fx_converted", "tolerance_bps": 5}])
        )


def test_each_operand_reads_only_its_own_side() -> None:
    wrong = {"condition": "text", "op": "equals", "left": col("b.reference"), "right": col("b.reference")}
    with pytest.raises(DslTypeError):
        compile_rule(compiled_rule("1:1", [key(), wrong]))


def test_null_and_empty_keys_never_match() -> None:
    c = compile_rule(compiled_rule("1:1", [key()]))
    assert left_key(c, view("a", {"reference": "R1"}), CALENDARS) == ("R1",)
    assert left_key(c, view("a", {"reference": ""}), CALENDARS) is None
    assert right_key(c, view("b", {}), CALENDARS) is None


def _totals(conditions: list[Any], left: int, right: int, kind: str = "1:1", **extra: Any) -> bool:
    c = compile_rule(compiled_rule(kind, [key(), *conditions], **extra))
    vl = values(c, "left", view("a", {"amount_minor": left}), CALENDARS)
    vr = values(c, "right", view("b", {"amount_minor": right}), CALENDARS)
    assert vl is not None
    assert vr is not None
    return totals_ok(c, vl, vr)


@pytest.mark.parametrize(
    ("condition", "left", "right", "ok"),
    [
        (amount(), 1000, 1000, True),
        (amount(), 1000, 1001, False),
        (amount("absolute", absolute_minor=5), 1000, 1005, True),
        (amount("absolute", absolute_minor=5), 1000, 994, False),
        (amount("bps", bps=100), 10_000, 10_100, True),  # 1% of the one side
        (amount("bps", bps=100), 10_000, 10_101, False),
        (amount("bps_capped", bps=100, cap_minor=50), 10_000, 10_060, False),  # the cap wins
        (amount("bps_capped", bps=100, cap_minor=50), 10_000, 10_050, True),
    ],
)
def test_amount_tolerances(condition: dict[str, Any], left: int, right: int, ok: bool) -> None:
    assert _totals([condition], left, right) is ok


def test_signs() -> None:
    assert _totals([amount("absolute", absolute_minor=10**9), {"condition": "sign", "mode": "same"}], 5, 7)
    assert not _totals([amount("absolute", absolute_minor=10**9), {"condition": "sign", "mode": "same"}], 5, -7)
    assert _totals([amount("absolute", absolute_minor=10**9), {"condition": "sign", "mode": "opposite"}], 5, -7)
    assert _totals([amount(), {"condition": "sign", "mode": "absolute"}], 500, -500)
    assert not _totals([amount()], 500, -500)


def test_many_to_one_takes_basis_points_of_the_one_side() -> None:
    rule = ("M:1", {"group_by": [col("a.reference")]})
    condition = amount("bps", bps=100)
    # the one side is b (right): 1% of 10,000 allows 100, although the many side's total is larger
    assert _totals([condition], 10_100, 10_000, rule[0], **rule[1])
    assert not _totals([condition], 10_101, 10_000, rule[0], **rule[1])


def test_a_null_amount_keeps_the_item_out() -> None:
    c = compile_rule(compiled_rule("1:1", [key(), amount(right=col("b.number_1"))]))
    assert values(c, "right", view("b", {"amount_minor": 5}), CALENDARS) is None
