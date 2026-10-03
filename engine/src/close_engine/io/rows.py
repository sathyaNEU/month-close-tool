"""Python rows to Delta: the contract's Spark types, DataFrames built from rows, and insert-only MERGE.

Moved here from the generator (Stage 7b) so the posting job shares them; ``close_datagen.writer`` re-exports them.
"""

from __future__ import annotations

from functools import reduce
from typing import Any, Final

from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType,
    DataType,
    DateType,
    DecimalType,
    IntegerType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from close_contracts.resolver import TableResolver
from close_contracts.table_schemas import TableSchema
from close_engine.io.tables import delta_table

_SIMPLE: Final[dict[str, DataType]] = {
    "string": StringType(),
    "bigint": LongType(),
    "int": IntegerType(),
    "boolean": BooleanType(),
    "date": DateType(),
    "timestamp": TimestampType(),
    "map<string,string>": MapType(StringType(), StringType()),
    "map<string,bigint>": MapType(StringType(), LongType()),
}
COMMIT_METADATA_CONF: Final = "spark.databricks.delta.commitInfo.userMetadata"


def spark_type(name: str) -> DataType:
    if name.startswith("decimal("):
        digits, scale = name.removeprefix("decimal(").removesuffix(")").split(",")
        return DecimalType(int(digits), int(scale))
    return _SIMPLE[name]


def struct_of(schema: TableSchema) -> StructType:
    return StructType([StructField(c.name, spark_type(c.type), c.nullable) for c in schema.columns])


def frame(spark: SparkSession, schema: TableSchema, rows: list[dict[str, Any]]) -> DataFrame:
    names = [c.name for c in schema.columns]
    return spark.createDataFrame([tuple(row.get(n) for n in names) for row in rows], struct_of(schema))


def insert_missing(
    spark: SparkSession,
    resolver: TableResolver,
    schema: TableSchema,
    rows: list[dict[str, Any]],
    *,
    within: Column | None = None,
    metadata: str | None = None,
) -> None:
    """Insert-only MERGE on the natural key: rows already present are left alone, so a rerun writes nothing twice.

    ``within`` narrows the target (a partition predicate, ADR-0002 feature 14), and ``metadata`` is recorded as the
    commit's ``userMetadata``."""
    if not schema.natural_key:
        raise ValueError(f"{schema.table.key} has no natural key")
    condition = reduce(lambda a, b: a & b, [F.col(f"t.{k}") == F.col(f"s.{k}") for k in schema.natural_key])
    if within is not None:
        condition = condition & within
    source = frame(spark, schema, rows).alias("s")
    if metadata is not None:
        spark.conf.set(COMMIT_METADATA_CONF, metadata)
    try:
        target = delta_table(spark, resolver, schema.table).alias("t")
        target.merge(source, condition).whenNotMatchedInsertAll().execute()
    finally:
        if metadata is not None:
            spark.conf.unset(COMMIT_METADATA_CONF)
