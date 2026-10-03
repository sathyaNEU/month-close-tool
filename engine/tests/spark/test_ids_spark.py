"""Spark hash IDs must equal the Python reference for every shared test vector."""

from __future__ import annotations

import json
from datetime import date
from importlib import resources

import pytest
from pyspark.sql import Column, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import DecimalType, StructField, StructType, TimestampType

from close_contracts.ids import hash_id as py_hash_id
from close_engine.sparkfns.ids import check_id_columns, hash_id

pytestmark = pytest.mark.spark


def _vectors() -> list[dict[str, object]]:
    text = resources.files("close_contracts").joinpath("test_vectors/ids.json").read_text(encoding="utf-8")
    return list(json.loads(text))


def _literal(part: dict[str, object]) -> Column:
    t = part["t"]
    if t == "null":
        return F.lit(None).cast("string")
    if t == "str":
        return F.lit(part["v"]).cast("string")
    if t == "int":
        return F.lit(part["v"]).cast("bigint")
    if t == "bool":
        return F.lit(part["v"]).cast("boolean")
    if t == "date":
        return F.lit(date.fromisoformat(str(part["v"])))
    raise AssertionError(f"unknown part type {t}")


def test_every_shared_vector_matches(spark: SparkSession) -> None:
    vectors = _vectors()
    assert len(vectors) >= 50
    columns = [
        hash_id(str(v["ns"]), *[_literal(p) for p in v["parts"]]).alias(f"v{i}")  # type: ignore[attr-defined]
        for i, v in enumerate(vectors)
    ]
    row = spark.range(1).select(*columns).collect()[0]
    mismatches = [(i, v["expected"], row[f"v{i}"]) for i, v in enumerate(vectors) if row[f"v{i}"] != v["expected"]]
    assert mismatches == []


def test_ids_from_dataframe_columns_match_python(spark: SparkSession) -> None:
    rows = [("ms-1", "ds-a", "k1", 10, None), ("ms-1", "ds-a", "k2", -3, date(2026, 9, 1)), ("ms-2", None, "", 0, None)]
    df = spark.createDataFrame(rows, "ms string, ds string, key string, n bigint, d date")
    got = df.select(hash_id("itm", "ms", "ds", "key", "n", "d").alias("id")).collect()
    assert [r["id"] for r in got] == [py_hash_id("itm", *r) for r in rows]


def test_disallowed_column_types_are_rejected(spark: SparkSession) -> None:
    df = spark.createDataFrame(
        [], StructType([StructField("ts", TimestampType()), StructField("dec", DecimalType(10, 2))])
    )
    for name in ("ts", "dec"):
        with pytest.raises(TypeError, match="not allowed in hash IDs"):
            check_id_columns(df, name)
    with pytest.raises(KeyError):
        check_id_columns(df, "missing")
