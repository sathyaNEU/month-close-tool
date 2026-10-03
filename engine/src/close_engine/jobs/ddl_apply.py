"""ddl_apply: apply the packaged ``V###`` Delta migrations to the lake, each exactly once.

It reads ``ops.schema_migrations`` (empty on a new lake), refuses to run if a migration that was already
applied has a different checksum today (history is never edited, a change needs a new file), then applies
every missing migration in version order and records it. Each statement is idempotent
(``CREATE TABLE IF NOT EXISTS``), so a run interrupted halfway is completed by the next one, and a rerun on
an up-to-date lake applies nothing.

    close-job ddl_apply --run-id ... --root-uri s3a://... [contract args]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pyspark.sql import Row, SparkSession
from pyspark.sql.types import IntegerType, StringType, StructField, StructType, TimestampType

from close_contracts.resolver import PathTableResolver, TableResolver
from close_contracts.tables import Tables
from close_engine import __version__ as ENGINE_VERSION
from close_engine.io.migrations import Migration, load, tables_in
from close_engine.io.sql import run_ddl
from close_engine.io.tables import read_table, save, table_exists
from close_engine.jobs import RESULT_PREFIX
from close_engine.runctx import RunContext, add_contract_args, context_from_args
from close_engine.session import get_session

EXIT_CHANGED: Final = 3
MIGRATIONS_SCHEMA: Final = StructType(
    [
        StructField("version", IntegerType(), False),
        StructField("name", StringType(), False),
        StructField("checksum", StringType(), False),
        StructField("statements", IntegerType(), False),
        StructField("applied_at", TimestampType(), False),
        StructField("run_id", StringType(), False),
        StructField("engine_version", StringType(), False),
    ]
)


def applied_checksums(spark: SparkSession, resolver: TableResolver) -> dict[int, str]:
    if not table_exists(spark, resolver, Tables.OPS_SCHEMA_MIGRATIONS):
        return {}
    rows = read_table(spark, resolver, Tables.OPS_SCHEMA_MIGRATIONS).select("version", "checksum").collect()
    return {int(r["version"]): str(r["checksum"]) for r in rows}


def apply_one(spark: SparkSession, resolver: TableResolver, migration: Migration, ctx: RunContext) -> None:
    for statement in migration.statements:
        run_ddl(spark, statement, resolver, **tables_in(statement))
    record = Row(
        version=migration.version,
        name=migration.name,
        checksum=migration.checksum,
        statements=len(migration.statements),
        applied_at=ctx.requested_at,
        run_id=ctx.run_id,
        engine_version=ENGINE_VERSION,
    )
    writer = (
        spark.createDataFrame([record], MIGRATIONS_SCHEMA)
        .write.format("delta")
        .mode("append")
        .option("userMetadata", ctx.commit_metadata())
    )
    save(writer, resolver, Tables.OPS_SCHEMA_MIGRATIONS)


def apply_all(spark: SparkSession, resolver: TableResolver, ctx: RunContext) -> dict[str, object]:
    migrations = load()
    applied = applied_checksums(spark, resolver)
    changed = sorted(m.version for m in migrations if m.version in applied and applied[m.version] != m.checksum)
    unknown = sorted(set(applied) - {m.version for m in migrations})
    if changed or unknown:
        return {"ok": False, "changed": changed, "unknown": unknown, "applied": []}
    done = []
    for migration in migrations:
        if migration.version not in applied:
            apply_one(spark, resolver, migration, ctx)
            done.append(migration.version)
    return {"ok": True, "changed": [], "unknown": [], "applied": done, "already": sorted(applied)}


def _parse(argv: Sequence[str] | None) -> tuple[RunContext, Path | None]:
    parser = argparse.ArgumentParser(prog="close-job ddl_apply", description=__doc__)
    add_contract_args(parser)
    parser.add_argument("--result-file", type=Path, default=None, help="also write the JSON result here")
    args = parser.parse_args(argv)
    return context_from_args(parser, args), args.result_file


def main(argv: Sequence[str] | None = None) -> int:
    ctx, result_file = _parse(argv)
    spark = get_session("close-ddl-apply")
    resolver = PathTableResolver(lake_root=ctx.root_uri)
    started = time.monotonic()
    outcome = apply_all(spark, resolver, ctx)
    result = {
        "job": "ddl_apply",
        "run_id": ctx.run_id,
        "attempt": ctx.attempt,
        "fencing_token": ctx.fencing_token,
        "engine_version": ENGINE_VERSION,
        "seconds": round(time.monotonic() - started, 1),
        **outcome,
    }
    line = json.dumps(result, sort_keys=True)
    sys.stdout.write(f"{RESULT_PREFIX}{line}\n")
    sys.stdout.flush()
    if result_file is not None:
        result_file.write_text(line + "\n", encoding="utf-8")
    return 0 if outcome["ok"] else EXIT_CHANGED
