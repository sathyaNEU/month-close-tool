"""Spark FX conversion must reproduce the Python reference exactly (incl. half-even ties)."""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import DecimalType

from close_contracts.money import scale_shift
from close_engine.sparkfns.money import AMOUNT_TYPE, RATE_TYPE, convert_minor, pow10

pytestmark = pytest.mark.spark


def test_every_money_vector_matches(spark: SparkSession) -> None:
    cases = json.loads(
        resources.files("close_contracts").joinpath("test_vectors/money.json").read_text(encoding="utf-8")
    )
    columns = [
        convert_minor(F.lit(c["amount_minor"]).cast("bigint"), F.lit(c["rate"]), scale_shift(c["from"], c["to"])).alias(
            f"c{i}"
        )
        for i, c in enumerate(cases)
    ]
    row = spark.range(1).select(*columns).collect()[0]
    assert [row[f"c{i}"] for i in range(len(cases))] == [c["expected_minor"] for c in cases]


def test_decimal_product_type_is_exact_38_12(spark: SparkSession) -> None:
    df = spark.range(1).select((F.lit(1).cast(AMOUNT_TYPE) * F.lit("1.5").cast(RATE_TYPE)).alias("p"))
    assert df.schema["p"].dataType == DecimalType(38, 12)


def test_pow10_literals(spark: SparkSession) -> None:
    row = spark.range(1).select(pow10(2).alias("a"), pow10(-3).alias("b"), pow10(0).alias("c")).collect()[0]
    assert (row["a"], row["b"], row["c"]) == (Decimal(100), Decimal("0.001"), Decimal(1))
    with pytest.raises(ValueError, match="outside"):
        pow10(9)


def test_overflow_raises_under_ansi(spark: SparkSession) -> None:
    df = spark.range(1).select(convert_minor(F.lit(10**17).cast("bigint"), F.lit("9999999.999999999999"), 4).alias("x"))
    with pytest.raises(Exception, match=r"(?i)overflow|out of range|cannot be represented"):
        df.collect()
