"""The V### migrations add up to the schema registry, apply once, and refuse edited history (Gate 2b)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pyspark.sql import SparkSession
from pyspark.sql.types import StructType

from close_contracts.resolver import PathTableResolver
from close_contracts.table_schemas import ALL_TABLES, REGISTRY, TableSchema
from close_contracts.tables import Tables
from close_engine.io.fence import CLAIMS_DDL
from close_engine.io.manifest import MANIFEST_DDL
from close_engine.io.migrations import Migration, load, split_statements, tables_in
from close_engine.io.sql import run_ddl
from close_engine.io.tables import delta_table, read_table, save
from close_engine.jobs.ddl_apply import applied_checksums, apply_all
from close_engine.jobs.smoke import build_rows
from close_engine.runctx import RunContext

pytestmark = pytest.mark.spark


def _ctx(root: Path, run: str = "ddl") -> RunContext:
    return RunContext(
        run_id=run,
        attempt=1,
        lease_resource="ddl:lake",
        fencing_token=1,
        requested_at=datetime(2026, 9, 28, tzinfo=UTC),
        root_uri=root.as_uri(),
        request_id=None,
    )


@pytest.fixture(scope="module")
def lake(spark: SparkSession, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PathTableResolver, float]:
    root = tmp_path_factory.mktemp("lake")
    started = time.monotonic()
    outcome = apply_all(spark, PathTableResolver(lake_root=root.as_uri()), _ctx(root))
    seconds = time.monotonic() - started
    assert outcome["ok"], outcome
    assert outcome["applied"] == [m.version for m in load()]
    return root, PathTableResolver(lake_root=root.as_uri()), seconds


def _actual(
    spark: SparkSession, resolver: PathTableResolver, schema: TableSchema
) -> tuple[StructType, list[str], dict[str, str]]:
    detail = delta_table(spark, resolver, schema.table).detail().collect()[0]
    return (
        read_table(spark, resolver, schema.table).schema,
        list(detail["partitionColumns"]),
        dict(detail["properties"]),
    )


def test_every_registered_table_matches_its_contract(
    spark: SparkSession, lake: tuple[Path, PathTableResolver, float]
) -> None:
    _, resolver, _ = lake
    mismatches = []
    for schema in ALL_TABLES:
        struct, partitions, properties = _actual(spark, resolver, schema)
        actual = [(f.name, f.dataType.simpleString(), f.nullable) for f in struct.fields]
        wanted = [(c.name, c.type, c.nullable) for c in schema.columns]
        if actual != wanted:
            mismatches.append((schema.table.key, "columns", actual, wanted))
        if partitions != list(schema.partitioned_by):
            mismatches.append((schema.table.key, "partitions", partitions, schema.partitioned_by))
        if (properties.get("delta.appendOnly") == "true") != schema.append_only:
            mismatches.append((schema.table.key, "appendOnly", properties.get("delta.appendOnly"), schema.append_only))
    assert not mismatches, mismatches
    assert len(ALL_TABLES) == len(REGISTRY) >= 45


def test_ddl_apply_is_fast_and_a_rerun_applies_nothing(
    spark: SparkSession, lake: tuple[Path, PathTableResolver, float]
) -> None:
    root, resolver, seconds = lake
    assert seconds <= 60, f"ddl_apply took {seconds:.1f}s on an empty lake"
    again = apply_all(spark, resolver, _ctx(root, "ddl-rerun"))
    assert again["ok"]
    assert again["applied"] == []
    assert sorted(applied_checksums(spark, resolver)) == [m.version for m in load()]


def test_an_edited_migration_is_refused(spark: SparkSession, tmp_path: Path) -> None:
    resolver = PathTableResolver(lake_root=tmp_path.as_uri())
    ctx = _ctx(tmp_path)
    first = load()[0]
    for statement in first.statements:
        run_ddl(spark, statement, resolver, **tables_in(statement))
    from close_engine.jobs.ddl_apply import MIGRATIONS_SCHEMA

    record = [(1, first.name, "0" * 64, len(first.statements), ctx.requested_at, "old", "0")]
    save(
        spark.createDataFrame(record, MIGRATIONS_SCHEMA).write.format("delta").mode("append"),
        resolver,
        Tables.OPS_SCHEMA_MIGRATIONS,
    )
    outcome = apply_all(spark, resolver, ctx)
    assert outcome == {"ok": False, "changed": [1], "unknown": [], "applied": []}


def test_v001_keeps_the_stage_1_tables_identical(
    spark: SparkSession, lake: tuple[Path, PathTableResolver, float], tmp_path: Path
) -> None:
    _, migrated, _ = lake
    legacy = PathTableResolver(lake_root=tmp_path.as_uri())
    run_ddl(spark, CLAIMS_DDL, legacy, t=Tables.OPS_WRITER_CLAIMS)
    run_ddl(spark, MANIFEST_DDL, legacy, t=Tables.OPS_RUN_MANIFESTS)
    for table in (Tables.OPS_WRITER_CLAIMS, Tables.OPS_RUN_MANIFESTS):
        schema = REGISTRY[table]
        assert _actual(spark, migrated, schema)[:2] == _actual(spark, legacy, schema)[:2]
    rows = build_rows(spark, "r" * 32, 50)
    writer = (
        rows.write.format("delta")
        .mode("overwrite")
        .option("replaceWhere", f"run_id = '{'r' * 32}'")
        .partitionBy("run_id")
    )
    save(writer, migrated, Tables.OPS_SMOKE_RESULTS)  # the smoke job's own write works on the migrated table
    assert read_table(spark, migrated, Tables.OPS_SMOKE_RESULTS).count() == 50


def test_statement_splitting_and_placeholders() -> None:
    text = (
        "-- note; with a semicolon\n"
        "CREATE TABLE IF NOT EXISTS {ops__run_metrics} (a STRING COMMENT 'x; y');\n\n"
        "ALTER TABLE {ops__run_metrics} SET TBLPROPERTIES ('k' = 'v');\n"
    )
    parts = split_statements(text)
    assert len(parts) == 2
    assert parts[0].endswith("COMMENT 'x; y')")
    assert tables_in(parts[1]) == {"ops__run_metrics": REGISTRY_KEY("ops.run_metrics")}
    with pytest.raises(ValueError, match="not a registered table"):
        tables_in("CREATE TABLE {ops__nope} (a INT)")


def REGISTRY_KEY(key: str) -> object:  # noqa: N802 (reads like a constant lookup in the assertion)
    return next(t for t in REGISTRY if t.key == key)


def test_migration_versions_are_contiguous_and_checksummed() -> None:
    migrations = load()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert all(isinstance(m, Migration) and len(m.checksum) == 64 for m in migrations)
    statements = [st for m in migrations for st in m.statements]
    creates = [st for st in statements if st.startswith("CREATE TABLE IF NOT EXISTS")]
    alters = [st for st in statements if st.startswith("ALTER TABLE")]
    assert len(creates) == len(ALL_TABLES)  # each table is created exactly once...
    assert len(creates) + len(alters) == len(statements)  # ...and only evolved by ALTER TABLE (V008 onwards)
    assert all(len(tables_in(st)) == 1 for st in alters)  # each on one registered table
