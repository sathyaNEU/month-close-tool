"""Run manifests: one row per (run, attempt, fencing token, entity) with that entity's row count and
content digest, appended after the job's results commit.

The app shows results by reading this table through the read path (a QuerySpec scoped to the caller's
entities), so what a user sees comes from the lake, not from the job's output. The table is append-only,
and every commit carries the writer's run, attempt and fencing token in ``userMetadata``. Its columns
must match :data:`close_contracts.queryable.RUN_MANIFESTS` (a test compares them).
"""

from __future__ import annotations

from collections.abc import Mapping

from pyspark.sql import SparkSession
from pyspark.sql.types import LongType, StringType, StructField, StructType

from close_contracts.resolver import TableResolver
from close_contracts.tables import Tables
from close_engine.io.sql import run_ddl
from close_engine.io.tables import save
from close_engine.runctx import RunContext

MANIFEST_DDL = (
    "CREATE TABLE IF NOT EXISTS {t} (run_id STRING NOT NULL, attempt BIGINT NOT NULL, fencing_token BIGINT NOT NULL, "
    "job STRING NOT NULL, entity_id STRING NOT NULL, row_count BIGINT NOT NULL, digest STRING NOT NULL) "
    "USING DELTA TBLPROPERTIES ('delta.appendOnly' = 'true')"
)
MANIFEST_SCHEMA = StructType(
    [
        StructField("run_id", StringType(), False),
        StructField("attempt", LongType(), False),
        StructField("fencing_token", LongType(), False),
        StructField("job", StringType(), False),
        StructField("entity_id", StringType(), False),
        StructField("row_count", LongType(), False),
        StructField("digest", StringType(), False),
    ]
)


def write_manifest(
    spark: SparkSession,
    resolver: TableResolver,
    ctx: RunContext,
    *,
    job: str,
    rows_by_entity: Mapping[str, int],
    digests_by_entity: Mapping[str, str],
) -> None:
    run_ddl(spark, MANIFEST_DDL, resolver, t=Tables.OPS_RUN_MANIFESTS)
    values = [
        (
            ctx.run_id,
            ctx.attempt,
            ctx.fencing_token,
            job,
            entity,
            int(rows_by_entity[entity]),
            digests_by_entity[entity],
        )
        for entity in sorted(digests_by_entity)
    ]
    writer = (
        spark.createDataFrame(values, MANIFEST_SCHEMA)
        .write.format("delta")
        .mode("append")
        .option("userMetadata", ctx.commit_metadata())
    )
    save(writer, resolver, Tables.OPS_RUN_MANIFESTS)
