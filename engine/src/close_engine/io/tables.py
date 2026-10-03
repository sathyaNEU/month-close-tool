"""Typed access to Delta tables through a TableResolver (paths locally, Unity Catalog names in prod).

Engine code reads and writes tables only through these helpers, so the same job runs unchanged against
path-based tables on the local S3-compatible store and against catalog tables on Databricks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyspark.sql import DataFrame, SparkSession

from close_contracts.resolver import PathTableResolver, TableResolver
from close_contracts.tables import LogicalTable

if TYPE_CHECKING:
    from pyspark.sql.readwriter import DataFrameWriter


def delta_table(spark: SparkSession, resolver: TableResolver, t: LogicalTable) -> Any:
    """Return a ``delta.tables.DeltaTable`` for ``t`` (merge / update / delete / history / optimize)."""
    from delta.tables import DeltaTable

    if isinstance(resolver, PathTableResolver):
        return DeltaTable.forPath(spark, resolver.location(t))
    return DeltaTable.forName(spark, resolver.table(t))


def read_table(
    spark: SparkSession, resolver: TableResolver, t: LogicalTable, *, version: int | None = None
) -> DataFrame:
    """Read ``t`` as a DataFrame, optionally pinned to a Delta version (time travel)."""
    reader = spark.read.format("delta")
    if version is not None:
        if version < 0:
            raise ValueError(f"invalid Delta version {version}")
        reader = reader.option("versionAsOf", version)
    if isinstance(resolver, PathTableResolver):
        return reader.load(resolver.location(t))
    return reader.table(resolver.table(t))


def current_version(spark: SparkSession, resolver: TableResolver, t: LogicalTable) -> int:
    """Latest committed Delta version of ``t`` (used to pin run inputs)."""
    row = delta_table(spark, resolver, t).history(1).select("version").collect()[0]
    return int(row["version"])


def save(writer: DataFrameWriter, resolver: TableResolver, t: LogicalTable) -> None:
    """Finish a configured ``df.write.format("delta")...`` chain against ``t``."""
    if isinstance(resolver, PathTableResolver):
        writer.save(resolver.location(t))
    else:
        writer.saveAsTable(resolver.table(t))


def table_exists(spark: SparkSession, resolver: TableResolver, t: LogicalTable) -> bool:
    """Whether ``t`` exists as a Delta table (a path locally, a catalog table on Databricks)."""
    from delta.tables import DeltaTable

    if isinstance(resolver, PathTableResolver):
        return bool(DeltaTable.isDeltaTable(spark, resolver.location(t)))
    return bool(spark.catalog.tableExists(resolver.table(t)))
