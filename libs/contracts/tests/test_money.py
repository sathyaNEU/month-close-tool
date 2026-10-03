from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from importlib import resources

import pytest
from hypothesis import given
from hypothesis import strategies as st

from close_contracts.money import (
    CURRENCY_EXPONENTS,
    MAX_MINOR_ABS,
    AmountRangeError,
    MoneyError,
    PrecisionError,
    RateError,
    UnknownCurrencyError,
    check_minor,
    check_rate,
    convert_minor,
    exponent,
    from_minor,
    round_half_even,
    scale_shift,
    to_minor,
)


def _reference_convert(amount_minor: int, from_ccy: str, to_ccy: str, rate: str) -> int:
    """Independent implementation with Fraction and explicit half-even rounding (no Decimal)."""
    exact = (
        Fraction(amount_minor)
        * Fraction(rate)
        * Fraction(10) ** (CURRENCY_EXPONENTS[to_ccy] - CURRENCY_EXPONENTS[from_ccy])
    )
    floor = exact.numerator // exact.denominator
    remainder = exact - floor
    if remainder > Fraction(1, 2):
        return floor + 1
    if remainder < Fraction(1, 2):
        return floor
    return floor if floor % 2 == 0 else floor + 1


def test_iso_exponents() -> None:
    assert exponent("USD") == 2
    assert exponent("JPY") == 0
    assert exponent("KWD") == 3
    assert exponent("CLF") == 4
    with pytest.raises(UnknownCurrencyError):
        exponent("XXX")


@pytest.mark.parametrize(
    ("amount", "ccy", "minor"),
    [
        ("12.34", "USD", 1234),
        ("-0.01", "USD", -1),
        ("100", "JPY", 100),
        ("1.234", "KWD", 1234),
        (5, "EUR", 500),
        (Decimal("0.10"), "GBP", 10),
    ],
)
def test_to_minor_is_exact(amount: str | int | Decimal, ccy: str, minor: int) -> None:
    assert to_minor(amount, ccy) == minor
    assert to_minor(from_minor(minor, ccy), ccy) == minor


@pytest.mark.parametrize(("amount", "ccy"), [("12.345", "USD"), ("0.5", "JPY"), ("1.2345", "KWD")])
def test_to_minor_rejects_excess_precision(amount: str, ccy: str) -> None:
    with pytest.raises(PrecisionError):
        to_minor(amount, ccy)


@pytest.mark.parametrize("bad", [1.25, "abc", "NaN", "Infinity"])
def test_to_minor_rejects_non_decimals(bad: object) -> None:
    with pytest.raises(MoneyError):
        to_minor(bad, "USD")  # type: ignore[arg-type]


def test_from_minor_keeps_currency_scale() -> None:
    assert str(from_minor(1200, "USD")) == "12.00"
    assert str(from_minor(7, "KWD")) == "0.007"
    assert str(from_minor(5, "JPY")) == "5"


def test_amount_range() -> None:
    assert check_minor(MAX_MINOR_ABS) == MAX_MINOR_ABS
    with pytest.raises(AmountRangeError):
        check_minor(MAX_MINOR_ABS + 1)
    with pytest.raises(AmountRangeError):
        check_minor(True)
    with pytest.raises(AmountRangeError):
        to_minor("10000000000000000", "USD")


@pytest.mark.parametrize("bad", ["0", "-1.5", "10000000", "1.0000000000001"])
def test_rate_validation(bad: str) -> None:
    with pytest.raises(RateError):
        check_rate(bad)


def test_rate_accepts_max_scale() -> None:
    assert check_rate("9999999.999999999999") == Decimal("9999999.999999999999")


def test_half_even_rounding() -> None:
    assert [round_half_even(Decimal(x)) for x in ["0.5", "1.5", "2.5", "-0.5", "-1.5", "-2.5", "2.4999"]] == [
        0,
        2,
        2,
        0,
        -2,
        -2,
        2,
    ]


def test_scale_shift() -> None:
    assert scale_shift("USD", "JPY") == -2
    assert scale_shift("JPY", "KWD") == 3


def test_vectors_file_matches_implementation_and_reference() -> None:
    text = resources.files("close_contracts").joinpath("test_vectors/money.json").read_text(encoding="utf-8")
    for case in json.loads(text):
        args = (case["amount_minor"], case["from"], case["to"], case["rate"])
        assert convert_minor(*args) == case["expected_minor"]
        assert _reference_convert(*args) == case["expected_minor"]


_ccy = st.sampled_from(sorted(CURRENCY_EXPONENTS))
_rate = st.decimals(min_value=Decimal("0.000000000001"), max_value=Decimal("9999999.999999999999"), places=12)


@given(st.integers(-(10**15), 10**15), _ccy, _ccy, _rate)
def test_convert_matches_independent_fraction_implementation(amount: int, f: str, t: str, rate: Decimal) -> None:
    try:
        result = convert_minor(amount, f, t, rate)
    except AmountRangeError:
        return  # result legitimately exceeds DECIMAL(18,0)
    assert result == _reference_convert(amount, f, t, str(rate))
