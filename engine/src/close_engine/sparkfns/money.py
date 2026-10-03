"""Spark implementation of :func:`close_contracts.money.convert_minor`.

``CAST(amount AS DECIMAL(18,0)) * CAST(rate AS DECIMAL(19,12))`` has result type DECIMAL(38,12) under
``spark.sql.decimalOperations.allowPrecisionLoss=false``, so the product is exact. The currency
exponent shift is applied as a second exact multiplication, and ``bround`` rounds half-to-even,
matching Python's ``ROUND_HALF_EVEN``. With ANSI mode on, a value that does not fit raises an error
instead of silently becoming NULL.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from pyspark.sql import Column
from pyspark.sql import functions as F
from pyspark.sql.types import DecimalType, LongType

from close_contracts.money import AMOUNT_PRECISION, RATE_PRECISION, RATE_SCALE

AMOUNT_TYPE: Final = DecimalType(AMOUNT_PRECISION, 0)
RATE_TYPE: Final = DecimalType(RATE_PRECISION, RATE_SCALE)
MAX_SHIFT: Final = 8


def pow10(shift: int) -> Column:
    """Exact decimal literal 10**shift (shift may be negative)."""
    if not -MAX_SHIFT <= shift <= MAX_SHIFT:
        raise ValueError(f"exponent shift {shift} is outside [-{MAX_SHIFT}, {MAX_SHIFT}]")
    if shift >= 0:
        return F.lit(str(10**shift)).cast(DecimalType(shift + 1, 0))
    return F.lit(str(Decimal(1).scaleb(shift))).cast(DecimalType(-shift + 1, -shift))


def convert_minor(amount_minor: Column, rate: Column, shift: int) -> Column:
    """Convert minor units at ``rate`` with a currency-exponent ``shift``; half-even rounding to BIGINT."""
    product = amount_minor.cast(AMOUNT_TYPE) * rate.cast(RATE_TYPE)
    if shift:
        product = product * pow10(shift)
    return F.bround(product, 0).cast(LongType())
