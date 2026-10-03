"""Output digests are order/partition independent; the session carries the pinned configuration."""

from __future__ import annotations

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from close_engine.io.digest import dataframe_digest
from close_engine.session import PINNED_CONF, apply_pinned_conf

pytestmark = pytest.mark.spark


def _sample(spark: SparkSession):  # type: ignore[no-untyped-def]
    rows = [(i, f"k{i % 7}", i * 13 - 50, None if i % 5 == 0 else f"ref{i}") for i in range(200)]
    df = spark.createDataFrame(rows, "id bigint, k string, amount_minor bigint, ref string")
    return df.withColumn("attrs", F.create_map(F.lit("z"), F.col("k"), F.lit("a"), F.col("ref")))


def test_digest_ignores_order_and_partitioning(spark: SparkSession) -> None:
    df = _sample(spark)
    base = dataframe_digest(df)
    assert dataframe_digest(df.repartition(13)) == base
    assert dataframe_digest(df.orderBy(F.col("id").desc())) == base
    assert dataframe_digest(df.coalesce(1)) == base
    swapped_map = df.withColumn("attrs", F.create_map(F.lit("a"), F.col("ref"), F.lit("z"), F.col("k")))
    assert dataframe_digest(swapped_map) == base


def test_digest_detects_any_change(spark: SparkSession) -> None:
    df = _sample(spark)
    base = dataframe_digest(df)
    assert dataframe_digest(df.where("id != 17")) != base
    assert (
        dataframe_digest(df.withColumn("amount_minor", F.when(F.col("id") == 3, 0).otherwise(F.col("amount_minor"))))
        != base
    )
    assert dataframe_digest(df.union(df.where("id = 3"))) != base  # duplicates count
    assert dataframe_digest(df, exclude=["ref"]) != base


def test_digest_of_empty_frame_is_stable(spark: SparkSession) -> None:
    empty = spark.createDataFrame([], "id bigint")
    assert dataframe_digest(empty) == dataframe_digest(empty.repartition(3))
    with pytest.raises(ValueError, match="no columns"):
        dataframe_digest(empty, exclude=["id"])


def test_session_is_pinned_and_drift_is_corrected(spark: SparkSession) -> None:
    for key, value in PINNED_CONF.items():
        assert str(spark.conf.get(key)).lower() == value.lower()
    spark.conf.set("spark.sql.session.timeZone", "America/New_York")
    assert apply_pinned_conf(spark)["spark.sql.session.timeZone"] == "UTC"
    assert spark.sql("SELECT current_timezone() AS tz").collect()[0]["tz"] == "UTC"


def test_integer_slash_is_double_and_div_is_exact(spark: SparkSession) -> None:
    # Documents why the banned-API lint forbids `/` in engine code.
    row = spark.sql("SELECT 7 / 2 AS slash, 7 div 2 AS d").collect()[0]
    assert row["slash"] == 3.5
    assert row["d"] == 3
    types = dict(spark.sql("SELECT 7 / 2 AS slash, 7 div 2 AS d").dtypes)
    assert types == {"slash": "double", "d": "bigint"}
