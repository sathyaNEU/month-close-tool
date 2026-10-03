"""Writer fencing inside jobs (detection, defence in depth; Stage 1 design rule 2).

The control plane prevents two writers by granting a lease only after the previous attempt is
terminal. Fencing catches what slips through (for example a "Repair run" with stale parameters, or a
zombie driver): every job appends a claim with its token as its first action, checks before each
commit that no higher token has been claimed, and checks once more after its last commit.

The window between the last check and the commit cannot be closed with separate Delta tables, so a
zombie that passes its check can still commit. That residual window is pinned by a strict-xfail test
and documented in the S5' ADR. The claims table is append-only.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import IntegerType, LongType, StringType, StructField, StructType, TimestampType

from close_contracts.resolver import TableResolver
from close_contracts.tables import Tables
from close_engine.io.sql import run_ddl
from close_engine.io.tables import read_table, save
from close_engine.runctx import RunContext

CLAIMS_DDL = (
    "CREATE TABLE IF NOT EXISTS {t} (resource STRING NOT NULL, fencing_token BIGINT NOT NULL, "
    "run_id STRING NOT NULL, attempt INT NOT NULL, job STRING NOT NULL, claimed_at TIMESTAMP NOT NULL) "
    "USING DELTA PARTITIONED BY (resource) TBLPROPERTIES ('delta.appendOnly' = 'true')"
)
CLAIM_SCHEMA = StructType(
    [
        StructField("resource", StringType(), False),
        StructField("fencing_token", LongType(), False),
        StructField("run_id", StringType(), False),
        StructField("attempt", IntegerType(), False),
        StructField("job", StringType(), False),
        StructField("claimed_at", TimestampType(), False),
    ]
)


class FencedError(RuntimeError):
    def __init__(self, stage: str, own: int, highest: int) -> None:
        self.stage, self.own, self.highest = stage, own, highest
        super().__init__(f"fenced at {stage}: own token {own} < highest claimed {highest}")


@dataclass
class Fence:
    spark: SparkSession
    resolver: TableResolver
    ctx: RunContext
    job: str

    def claim(self) -> None:
        run_ddl(self.spark, CLAIMS_DDL, self.resolver, t=Tables.OPS_WRITER_CLAIMS)
        row = [
            (
                self.ctx.lease_resource,
                self.ctx.fencing_token,
                self.ctx.run_id,
                self.ctx.attempt,
                self.job,
                self.ctx.requested_at,
            )
        ]
        writer = (
            self.spark.createDataFrame(row, CLAIM_SCHEMA)
            .write.format("delta")
            .mode("append")
            .option("userMetadata", self.ctx.commit_metadata())
        )
        save(writer, self.resolver, Tables.OPS_WRITER_CLAIMS)

    def highest(self) -> int:
        claims = read_table(self.spark, self.resolver, Tables.OPS_WRITER_CLAIMS)
        value = (
            claims.where(F.col("resource") == F.lit(self.ctx.lease_resource))
            .agg(F.max("fencing_token").alias("m"))
            .collect()[0]["m"]
        )
        return int(value or 0)

    def check(self, stage: str) -> None:
        """Raise FencedError if a newer writer has claimed this resource."""
        highest = self.highest()
        if highest > self.ctx.fencing_token:
            raise FencedError(stage, self.ctx.fencing_token, highest)
