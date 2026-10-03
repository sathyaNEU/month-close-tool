"""The narrow rate lookup on a real local Delta table (Gate 7c): the latest version of each key's row, read at
one pinned table version that each rate records, and nothing for a key without a row or a lake without the table.
The same rows through the reference lookup give the same answer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pyspark.sql import SparkSession
from pyspark.sql.types import DateType, DecimalType, IntegerType, StringType, StructField, StructType

from close_contracts.fx import RateKey
from close_contracts.resolver import PathTableResolver
from close_contracts.tables import Tables
from close_platform.query.rates import DataFrameRates, ReferenceRates

pytestmark = pytest.mark.spark
DAY = date(2026, 9, 15)
SCHEMA = StructType(
    [
        StructField("rate_date", DateType(), False),
        StructField("from_currency", StringType(), False),
        StructField("to_currency", StringType(), False),
        StructField("rate_type", StringType(), False),
        StructField("rate", DecimalType(19, 12), False),
        StructField("version", IntegerType(), False),
        StructField("source", StringType(), False),
    ]
)
ROWS = [
    (DAY, "EUR", "USD", "daily_spot", Decimal("1.080000000000"), 1, "generator"),
    (DAY, "EUR", "USD", "daily_spot", Decimal("1.081234567891"), 2, "generator"),  # republished
    (DAY, "EUR", "USD", "closing", Decimal("1.500000000000"), 1, "generator"),  # another type
    (date(2026, 9, 16), "EUR", "USD", "daily_spot", Decimal("1.090000000000"), 1, "generator"),
    (DAY, "GBP", "USD", "daily_spot", Decimal("1.270000000000"), 1, "generator"),
]


@pytest.fixture(scope="module")
def spark() -> SparkSession:
    from close_engine.session import LocalSparkOptions, build_local_session

    active = SparkSession.getActiveSession()
    return active or build_local_session(
        LocalSparkOptions(app_name="close-rates-tests", master="local[2]", driver_memory="1g", shuffle_partitions=2)
    )


def test_the_latest_version_at_one_pinned_table_version(spark: SparkSession, tmp_path: Path) -> None:
    resolver = PathTableResolver(lake_root=tmp_path.as_uri())
    path = resolver.location(Tables.REF_FX_RATES)
    spark.createDataFrame(ROWS[:1], SCHEMA).write.format("delta").save(path)
    spark.createDataFrame(ROWS[1:], SCHEMA).write.format("delta").mode("append").save(path)  # table version 1
    lookup = DataFrameRates(lambda work: work(spark), resolver)
    keys = [RateKey("EUR", "USD", DAY), RateKey("GBP", "USD", DAY), RateKey("USD", "EUR", DAY)]
    found = lookup.rates(keys)
    assert sorted(found) == sorted(keys[:2])  # no USD->EUR row
    eur = found[RateKey("EUR", "USD", DAY)]
    assert (eur.rate, eur.version, eur.source, eur.table_version) == (Decimal("1.081234567891"), 2, "generator", 1)
    reference = ReferenceRates([dict(zip([f.name for f in SCHEMA.fields], row, strict=True)) for row in ROWS]).rates(
        keys
    )
    assert {k: (r.rate, r.version) for k, r in reference.items()} == {k: (r.rate, r.version) for k, r in found.items()}
    assert lookup.rates([]) == {}


def test_no_table_means_no_rates(spark: SparkSession, tmp_path: Path) -> None:
    lookup = DataFrameRates(lambda work: work(spark), PathTableResolver(lake_root=tmp_path.as_uri()))
    assert lookup.rates([RateKey("EUR", "USD", DAY)]) == {}
