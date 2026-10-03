"""Writing generated rows to Delta tables through the engine's table helpers."""

from __future__ import annotations

from typing import Any, Final

from pyspark.sql import SparkSession

from close_contracts.resolver import TableResolver
from close_contracts.table_schemas import TableSchema
from close_engine.io.rows import frame, insert_missing, spark_type, struct_of
from close_engine.io.tables import save

__all__ = ["CHUNK", "frame", "insert_missing", "spark_type", "struct_of", "write"]
CHUNK: Final = 100_000


def write(
    spark: SparkSession,
    resolver: TableResolver,
    schema: TableSchema,
    rows: list[dict[str, Any]],
    *,
    metadata: str,
    append: bool = False,
) -> None:
    for n, start in enumerate(range(0, max(len(rows), 1), CHUNK)):
        # Large tables go to Spark in chunks so the driver never holds a second full copy of a table.
        writer = (
            frame(spark, schema, rows[start : start + CHUNK])
            .write.format("delta")
            .mode("append" if append or n > 0 else "overwrite")
            .option("userMetadata", metadata)
        )
        save(writer, resolver, schema.table)
