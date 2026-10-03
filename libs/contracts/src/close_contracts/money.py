"""Money in integer minor units, with exact FX conversion and banker's rounding.

Every amount in the platform is an integer count of the currency's minor unit (cents for USD, yen
for JPY, fils for KWD, ...), stored as BIGINT in Delta. That keeps sums exact, makes subset-sum
matching work on plain integers, and avoids float or pandas precision loss.

FX conversion is exact and then rounded half-to-even, identically in Python and Spark:

    target_minor = bround(amount_minor * rate * 10^(exp_to - exp_from), 0)

Spark evaluates ``CAST(amount AS DECIMAL(18,0)) * CAST(rate AS DECIMAL(19,12))``, whose result type
is exactly DECIMAL(38,12), so no digits are lost (see close_engine.sparkfns.money). The limits below
keep every value inside those types.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import Final

AMOUNT_PRECISION: Final = 18
"""Decimal digits available for a minor-unit amount (DECIMAL(18,0) in Spark)."""

MAX_MINOR_ABS: Final = 10**AMOUNT_PRECISION - 1

RATE_PRECISION: Final = 19
RATE_SCALE: Final = 12
MAX_RATE: Final = Decimal(10) ** (RATE_PRECISION - RATE_SCALE)
"""Exclusive upper bound for an FX rate (it must fit DECIMAL(19,12))."""

# ISO 4217 minor units for the currencies we expect a global marketplace to touch. Data-source
# mappings can still declare how a *source* expresses amounts (some PSPs treat HUF or TWD as
# zero-decimal); these exponents are the platform's canonical representation.
CURRENCY_EXPONENTS: Final = MappingProxyType(
    {
        **dict.fromkeys(
            [
                "AED",
                "AUD",
                "BRL",
                "CAD",
                "CHF",
                "CNY",
                "CZK",
                "DKK",
                "EUR",
                "GBP",
                "HKD",
                "HUF",
                "IDR",
                "ILS",
                "INR",
                "MXN",
                "MYR",
                "NOK",
                "NZD",
                "PHP",
                "PLN",
                "SAR",
                "SEK",
                "SGD",
                "THB",
                "TRY",
                "TWD",
                "USD",
                "ZAR",
            ],
            2,
        ),
        **dict.fromkeys(["CLP", "ISK", "JPY", "KRW", "PYG", "UGX", "VND", "XAF", "XOF"], 0),
        **dict.fromkeys(["BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"], 3),
        **dict.fromkeys(["CLF", "UYW"], 4),
    }
)


class MoneyError(ValueError):
    """Base class for money validation errors."""


class UnknownCurrencyError(MoneyError):
    """Raised for a currency code that is not in CURRENCY_EXPONENTS."""


class PrecisionError(MoneyError):
    """Raised when an amount has more decimal places than its currency allows."""


class AmountRangeError(MoneyError):
    """Raised when a minor-unit amount does not fit DECIMAL(18,0)."""


class RateError(MoneyError):
    """Raised when an FX rate is not positive or does not fit DECIMAL(19,12)."""


def exponent(currency: str) -> int:
    """Return the number of minor-unit decimal places for ``currency`` (ISO 4217)."""
    try:
        return CURRENCY_EXPONENTS[currency]
    except KeyError:
        raise UnknownCurrencyError(f"unknown or unsupported currency {currency!r}") from None


def check_minor(amount_minor: int) -> int:
    """Return ``amount_minor`` if it fits DECIMAL(18,0), else raise AmountRangeError."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise AmountRangeError(f"minor-unit amounts must be int, got {type(amount_minor).__name__}")
    if abs(amount_minor) > MAX_MINOR_ABS:
        raise AmountRangeError(f"amount {amount_minor} does not fit DECIMAL({AMOUNT_PRECISION},0)")
    return amount_minor


def _as_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, float):
        raise MoneyError("floats are not accepted for money; pass a Decimal, int or decimal string")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        raise MoneyError(f"not a decimal number: {value!r}") from None
    if not d.is_finite():
        raise MoneyError(f"not a finite number: {value!r}")
    return d


def to_minor(amount: Decimal | int | str, currency: str) -> int:
    """Convert a major-unit amount (e.g. ``"12.34"`` USD) to minor units, exactly.

    Raises PrecisionError if the amount has more decimal places than the currency allows; this is
    deliberate, because silently rounding source data would hide a mapping error.
    """
    scaled = _as_decimal(amount).scaleb(exponent(currency))
    if scaled != scaled.to_integral_value():
        raise PrecisionError(f"{amount} has more decimal places than {currency} allows")
    return check_minor(int(scaled))


def from_minor(amount_minor: int, currency: str) -> Decimal:
    """Convert minor units back to a major-unit Decimal with the currency's exact scale."""
    exp = exponent(currency)
    return Decimal(check_minor(amount_minor)).scaleb(-exp).quantize(Decimal(1).scaleb(-exp))


def check_rate(rate: Decimal | str) -> Decimal:
    """Validate an FX rate: positive and representable as DECIMAL(19,12) without rounding."""
    d = _as_decimal(rate)
    if d <= 0:
        raise RateError(f"FX rate must be positive, got {rate}")
    if d >= MAX_RATE:
        raise RateError(f"FX rate {rate} does not fit DECIMAL({RATE_PRECISION},{RATE_SCALE})")
    if d != d.quantize(Decimal(1).scaleb(-RATE_SCALE)):
        raise RateError(f"FX rate {rate} has more than {RATE_SCALE} decimal places")
    return d


def scale_shift(from_currency: str, to_currency: str) -> int:
    """Return exp(to) - exp(from): the power of ten applied when converting minor units."""
    return exponent(to_currency) - exponent(from_currency)


def round_half_even(value: Decimal) -> int:
    """Round a Decimal to the nearest integer, ties to even (Spark's ``bround(x, 0)``)."""
    return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))


def convert_minor(amount_minor: int, from_currency: str, to_currency: str, rate: Decimal | str) -> int:
    """Convert a minor-unit amount between currencies at ``rate`` (units of ``to`` per 1 ``from``).

    The product is computed exactly and then rounded half-to-even, matching
    ``close_engine.sparkfns.money.convert_minor`` bit for bit.
    """
    check_minor(amount_minor)
    r = check_rate(rate)
    shift = scale_shift(from_currency, to_currency)
    with localcontext() as ctx:
        ctx.prec = 60  # 18 amount digits + 19 rate digits + shift headroom, never rounds
        exact = Decimal(amount_minor) * r * Decimal(1).scaleb(shift)
        return check_minor(round_half_even(exact))
