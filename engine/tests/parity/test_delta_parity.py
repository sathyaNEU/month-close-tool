"""Spike S2: the portable Spark 4.1 / Delta 4.4 feature allowlist, proven on the S3-compatible store.

Each test is one allowlisted capability, exercised in exactly the form the engine will use it:
DataFrame / DeltaTable APIs for data and metadata operations, and ``run_ddl`` (resolver-rendered
identifiers, no parameters) for DDL. The gate runs the file three times; only 3/3 features are on the
allowlist (docs/adr/0002-spark-delta-parity-allowlist.md). SQL parameter binding is on the deny list
(delta-io/delta#7640) and is tracked by strict-xfail canaries in tests/spark/test_known_incompatibilities.py.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from close_contracts.enums import Schema
from close_contracts.resolver import PathTableResolver
from close_contracts.tables import LogicalTable
from close_engine.io.sql import DDLTemplateError, render, run_ddl
from close_engine.io.tables import current_version, delta_table, read_table, save

pytestmark = [pytest.mark.parity, pytest.mark.spark]

T = LogicalTable(Schema.MATCH, "parity_items")


@pytest.fixture
def resolver(s3_root: str, request: pytest.FixtureRequest) -> PathTableResolver:
    """A fresh lake root per test, on the S3-compatible store."""
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in request.node.name)
    return PathTableResolver(lake_root=f"{s3_root}/{safe}")


def _append(spark: SparkSession, resolver: PathTableResolver, rows: list[tuple[object, ...]], schema: str) -> None:
    save(spark.createDataFrame(rows, schema).write.format("delta").mode("append"), resolver, T)


def _rows(df: DataFrame, *order: str) -> list[tuple[object, ...]]:
    return [tuple(r) for r in df.orderBy(*order).collect()]


def test_01_merge_insert_update_delete_not_matched_by_source(spark: SparkSession, resolver: PathTableResolver) -> None:
    run_ddl(spark, "CREATE TABLE {t} (id BIGINT NOT NULL, v STRING) USING DELTA", resolver, t=T)
    _append(spark, resolver, [(1, "a"), (2, "b"), (3, "c"), (4, "d")], "id bigint, v string")
    src = spark.createDataFrame([(1, "u"), (2, "del"), (5, "new")], "id bigint, v string")
    (
        delta_table(spark, resolver, T)
        .alias("tgt")
        .merge(src.alias("src"), F.col("tgt.id") == F.col("src.id"))
        .whenMatchedDelete(condition=F.col("src.v") == F.lit("del"))
        .whenMatchedUpdate(set={"v": F.col("src.v")})
        .whenNotMatchedInsert(values={"id": F.col("src.id"), "v": F.col("src.v")})
        .whenNotMatchedBySourceDelete(condition=F.col("tgt.id") == F.lit(4))
        .execute()
    )
    assert _rows(read_table(spark, resolver, T), "id") == [(1, "u"), (3, "c"), (5, "new")]


def test_02_append_only_allows_insert_merge_and_rejects_update_delete(
    spark: SparkSession, resolver: PathTableResolver
) -> None:
    run_ddl(
        spark,
        "CREATE TABLE {t} (id BIGINT NOT NULL, v STRING) USING DELTA TBLPROPERTIES ('delta.appendOnly' = 'true')",
        resolver,
        t=T,
    )
    src = spark.createDataFrame([(1, "a"), (2, "b")], "id bigint, v string")

    def insert_if_absent() -> None:
        (
            delta_table(spark, resolver, T)
            .alias("tgt")
            .merge(src.alias("src"), F.col("tgt.id") == F.col("src.id"))
            .whenNotMatchedInsertAll()
            .execute()
        )

    insert_if_absent()
    insert_if_absent()  # idempotent
    assert _rows(read_table(spark, resolver, T), "id") == [(1, "a"), (2, "b")]
    with pytest.raises(Exception, match=r"(?i)append.?only"):
        delta_table(spark, resolver, T).update(condition=F.col("id") == 1, set={"v": F.lit("x")})
    with pytest.raises(Exception, match=r"(?i)append.?only"):
        delta_table(spark, resolver, T).delete(F.col("id") == 1)
    assert _rows(read_table(spark, resolver, T), "id") == [(1, "a"), (2, "b")]


def test_03_unexpected_column_is_rejected_when_automerge_is_off(
    spark: SparkSession, resolver: PathTableResolver
) -> None:
    _append(spark, resolver, [(1, "a")], "id bigint, v string")
    extra = spark.createDataFrame([(2, "b", "surprise")], "id bigint, v string, extra string")
    with pytest.raises(Exception, match=r"(?i)schema"):
        save(extra.write.format("delta").mode("append"), resolver, T)
    assert read_table(spark, resolver, T).columns == ["id", "v"]


def test_04_time_travel_and_version_pins(spark: SparkSession, resolver: PathTableResolver) -> None:
    run_ddl(spark, "CREATE TABLE {t} (id BIGINT) USING DELTA", resolver, t=T)  # v0
    _append(spark, resolver, [(1,)], "id bigint")  # v1
    _append(spark, resolver, [(2,), (3,)], "id bigint")  # v2
    assert current_version(spark, resolver, T) == 2
    assert read_table(spark, resolver, T, version=1).count() == 1
    assert read_table(spark, resolver, T, version=2).count() == 3
    assert read_table(spark, resolver, T, version=0).count() == 0


def test_05_change_data_feed(spark: SparkSession, resolver: PathTableResolver) -> None:
    run_ddl(
        spark,
        "CREATE TABLE {t} (id BIGINT, v STRING) USING DELTA TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')",
        resolver,
        t=T,
    )
    _append(spark, resolver, [(1, "a"), (2, "b")], "id bigint, v string")
    delta_table(spark, resolver, T).update(condition=F.col("id") == 1, set={"v": F.lit("a2")})
    delta_table(spark, resolver, T).delete(F.col("id") == 2)
    changes = (
        spark.read.format("delta")
        .option("readChangeFeed", "true")
        .option("startingVersion", 1)
        .load(resolver.location(T))
    )
    assert {r["_change_type"] for r in changes.select("_change_type").collect()} == {
        "insert",
        "update_preimage",
        "update_postimage",
        "delete",
    }


def test_06_check_and_not_null_constraints(spark: SparkSession, resolver: PathTableResolver) -> None:
    run_ddl(spark, "CREATE TABLE {t} (id BIGINT NOT NULL, amount_minor BIGINT) USING DELTA", resolver, t=T)
    run_ddl(spark, "ALTER TABLE {t} ADD CONSTRAINT amount_nonneg CHECK (amount_minor >= 0)", resolver, t=T)
    _append(spark, resolver, [(1, 10)], "id bigint, amount_minor bigint")
    with pytest.raises(Exception, match=r"(?i)constraint|amount_nonneg"):
        _append(spark, resolver, [(2, -1)], "id bigint, amount_minor bigint")
    with pytest.raises(Exception, match=r"(?i)null"):
        _append(spark, resolver, [(None, 5)], "id bigint, amount_minor bigint")
    with pytest.raises(Exception, match=r"(?i)already exists"):
        run_ddl(spark, "ALTER TABLE {t} ADD CONSTRAINT amount_nonneg CHECK (amount_minor >= 0)", resolver, t=T)
    assert _rows(read_table(spark, resolver, T), "id") == [(1, 10)]


def test_07_deletion_vectors(spark: SparkSession, resolver: PathTableResolver) -> None:
    run_ddl(
        spark,
        "CREATE TABLE {t} (id BIGINT) USING DELTA TBLPROPERTIES ('delta.enableDeletionVectors' = 'true')",
        resolver,
        t=T,
    )
    save(spark.range(100).coalesce(1).write.format("delta").mode("append"), resolver, T)
    delta_table(spark, resolver, T).delete(F.col("id") < 10)
    last = delta_table(spark, resolver, T).history(1).collect()[0]
    assert last["operation"] == "DELETE"
    assert int(last["operationMetrics"]["numDeletionVectorsAdded"]) >= 1
    assert read_table(spark, resolver, T).count() == 90


def test_08_row_tracking_ids_survive_updates(spark: SparkSession, resolver: PathTableResolver) -> None:
    run_ddl(
        spark,
        "CREATE TABLE {t} (id BIGINT, v STRING) USING DELTA TBLPROPERTIES ('delta.enableRowTracking' = 'true')",
        resolver,
        t=T,
    )
    _append(spark, resolver, [(1, "a"), (2, "b"), (3, "c")], "id bigint, v string")

    def row_ids() -> dict[int, int]:
        df = read_table(spark, resolver, T).select("id", F.col("_metadata.row_id").alias("row_id"))
        return {r["id"]: r["row_id"] for r in df.collect()}

    before = row_ids()
    delta_table(spark, resolver, T).update(condition=F.col("id") == 2, set={"v": F.lit("b2")})
    assert len(set(before.values())) == 3
    assert row_ids() == before


def test_09_ddl_gateway_renders_only_resolver_identifiers(spark: SparkSession, resolver: PathTableResolver) -> None:
    sql = render("CREATE TABLE {t} (id BIGINT) USING DELTA", resolver, t=T)
    assert sql == f"CREATE TABLE delta.`{resolver.location(T)}` (id BIGINT) USING DELTA"
    with pytest.raises(DDLTemplateError, match="placeholder mismatch"):
        render("CREATE TABLE {t} (id BIGINT) USING DELTA", resolver)
    with pytest.raises(DDLTemplateError, match="must be a LogicalTable"):
        render("CREATE TABLE {t} (id BIGINT) USING DELTA", resolver, t="delta.`s3a://x`")  # type: ignore[arg-type]
    with pytest.raises(DDLTemplateError, match="parameter markers"):
        render("CREATE TABLE {t} (id BIGINT DEFAULT :v) USING DELTA", resolver, t=T)
    for statement in ["SELECT * FROM {t}", "DROP TABLE {t}", "MERGE INTO {t} USING x ON true WHEN MATCHED THEN DELETE"]:
        with pytest.raises(DDLTemplateError, match="exactly one"):
            run_ddl(spark, statement, resolver, t=T)
    with pytest.raises(DDLTemplateError, match="exactly one"):
        run_ddl(spark, "CREATE TABLE {t} (id BIGINT) USING DELTA; DROP TABLE x", resolver, t=T)


def test_10_apply_in_pandas_under_ansi(spark: SparkSession) -> None:
    df = spark.createDataFrame(
        [("a", 5), ("a", 7), ("b", -3), ("b", 3), ("c", 9_000_000_000_000)], "k string, amount_minor bigint"
    )

    def total(pdf: pd.DataFrame) -> pd.DataFrame:
        pdf = pdf.sort_values("amount_minor", kind="mergesort")
        return pd.DataFrame({"k": [pdf["k"].iloc[0]], "total": [int(pdf["amount_minor"].sum())], "n": [len(pdf)]})

    out = df.groupBy("k").applyInPandas(total, schema="k string, total bigint, n bigint")
    assert _rows(out, "k") == [("a", 12, 2), ("b", 0, 2), ("c", 9_000_000_000_000, 1)]
    assert spark.conf.get("spark.sql.ansi.enabled") == "true"


def test_11_exact_decimal_round_trip_through_delta(spark: SparkSession, resolver: PathTableResolver) -> None:
    df = spark.createDataFrame([(123_456_789_012_345, "1.087654321098")], "amount_minor bigint, rate string").select(
        (F.col("amount_minor").cast("decimal(18,0)") * F.col("rate").cast("decimal(19,12)")).alias("p")
    )
    assert df.schema["p"].dataType.simpleString() == "decimal(38,12)"
    save(df.write.format("delta"), resolver, T)
    stored = read_table(spark, resolver, T).collect()[0]["p"]
    assert stored == Decimal("123456789012345") * Decimal("1.087654321098")


def test_12_bround_is_half_even(spark: SparkSession) -> None:
    values = ["0.5", "1.5", "2.5", "-0.5", "-1.5", "-2.5", "2.4999"]
    cols = [F.bround(F.lit(v).cast("decimal(10,4)"), 0).alias(f"c{i}") for i, v in enumerate(values)]
    row = spark.range(1).select(*cols).collect()[0]
    assert [int(row[f"c{i}"]) for i in range(len(values))] == [0, 2, 2, 0, -2, -2, 2]


def test_13_liquid_clustering(spark: SparkSession, resolver: PathTableResolver) -> None:
    run_ddl(spark, "CREATE TABLE {t} (k STRING, v BIGINT) USING DELTA CLUSTER BY (k)", resolver, t=T)
    _append(spark, resolver, [("b", 2), ("a", 1), ("c", 3)], "k string, v bigint")
    delta_table(spark, resolver, T).optimize().executeCompaction()
    detail = delta_table(spark, resolver, T).detail().collect()[0]
    assert list(detail["clusteringColumns"]) == ["k"]


def test_14_partition_scoped_merge_leaves_other_partitions_untouched(
    spark: SparkSession, resolver: PathTableResolver
) -> None:
    run_ddl(
        spark,
        "CREATE TABLE {t} (match_set_id STRING NOT NULL, id BIGINT NOT NULL, v STRING) USING DELTA "
        "PARTITIONED BY (match_set_id)",
        resolver,
        t=T,
    )
    _append(
        spark, resolver, [("ms1", 1, "a"), ("ms1", 2, "b"), ("ms2", 1, "x")], "match_set_id string, id bigint, v string"
    )

    def files(ms: str) -> set[str]:
        df = (
            read_table(spark, resolver, T)
            .where(F.col("match_set_id") == ms)
            .select(F.col("_metadata.file_path").alias("f"))
        )
        return {r["f"] for r in df.collect()}

    ms2_before = files("ms2")
    src = spark.createDataFrame([("ms1", 2, "b2"), ("ms1", 3, "c")], "match_set_id string, id bigint, v string")
    condition = (
        (F.col("tgt.match_set_id") == F.lit("ms1"))  # literal partition predicate on every MERGE
        & (F.col("tgt.match_set_id") == F.col("src.match_set_id"))
        & (F.col("tgt.id") == F.col("src.id"))
    )
    (
        delta_table(spark, resolver, T)
        .alias("tgt")
        .merge(src.alias("src"), condition)
        .whenMatchedUpdate(set={"v": F.col("src.v")})
        .whenNotMatchedInsertAll()
        .execute()
    )
    assert files("ms2") == ms2_before
    assert _rows(read_table(spark, resolver, T), "match_set_id", "id") == [
        ("ms1", 1, "a"),
        ("ms1", 2, "b2"),
        ("ms1", 3, "c"),
        ("ms2", 1, "x"),
    ]


def test_15_history_exposes_audit_columns(spark: SparkSession, resolver: PathTableResolver) -> None:
    run_ddl(spark, "CREATE TABLE {t} (id BIGINT) USING DELTA", resolver, t=T)
    _append(spark, resolver, [(1,)], "id bigint")
    history = delta_table(spark, resolver, T).history()
    expected = {
        "version",
        "timestamp",
        "userName",
        "operation",
        "operationParameters",
        "operationMetrics",
        "engineInfo",
    }
    assert expected <= set(history.columns)
    latest = history.orderBy(F.col("version").desc()).collect()[0]
    assert latest["operation"] == "WRITE"
    assert "Delta-Lake/4.4.0" in latest["engineInfo"]


def test_16_create_if_not_exists_is_idempotent_and_properties_stick(
    spark: SparkSession, resolver: PathTableResolver
) -> None:
    ddl = (
        "CREATE TABLE IF NOT EXISTS {t} (match_set_id STRING NOT NULL, id BIGINT NOT NULL) USING DELTA "
        "PARTITIONED BY (match_set_id) "
        "TBLPROPERTIES ('delta.appendOnly' = 'true', 'delta.enableChangeDataFeed' = 'true')"
    )
    run_ddl(spark, ddl, resolver, t=T)
    run_ddl(spark, ddl, resolver, t=T)
    props = delta_table(spark, resolver, T).detail().collect()[0]["properties"]
    assert props.get("delta.appendOnly") == "true"
    assert props.get("delta.enableChangeDataFeed") == "true"
    assert current_version(spark, resolver, T) == 0  # the second CREATE IF NOT EXISTS was a no-op


def test_17_session_timezone_is_utc(spark: SparkSession) -> None:
    assert spark.conf.get("spark.sql.session.timeZone") == "UTC"
    row = spark.range(1).select(F.to_date(F.lit("2026-09-30 23:30:00").cast("timestamp")).alias("d")).collect()[0]
    assert str(row["d"]) == "2026-09-30"


def test_18_commit_user_metadata_is_recorded_in_history(spark: SparkSession, resolver: PathTableResolver) -> None:
    meta = '{"attempt": 2, "fencing_token": 7, "run_id": "' + "c" * 32 + '"}'
    df = spark.createDataFrame([(1,)], "id bigint")
    save(df.write.format("delta").mode("append").option("userMetadata", meta), resolver, T)
    delta_table(spark, resolver, T).update(condition=F.col("id") == 1, set={"id": F.lit(2)})  # no metadata
    history = delta_table(spark, resolver, T).history().orderBy("version").collect()
    assert history[0]["userMetadata"] == meta
    assert history[-1]["userMetadata"] is None


def test_19_add_columns_evolves_an_append_only_table(spark: SparkSession, resolver: PathTableResolver) -> None:
    """Stage 7c (DDL V008): nullable columns added to an append-only table; old rows read them as null, insert-only
    MERGE fills them, and the table stays append-only."""
    run_ddl(
        spark,
        "CREATE TABLE {t} (id BIGINT NOT NULL, v STRING) USING DELTA TBLPROPERTIES ('delta.appendOnly' = 'true')",
        resolver,
        t=T,
    )
    _append(spark, resolver, [(1, "a")], "id bigint, v string")
    run_ddl(
        spark, "ALTER TABLE {t} ADD COLUMNS (rate DECIMAL(19,12) COMMENT 'added later', kind STRING)", resolver, t=T
    )
    src = spark.createDataFrame(
        [(1, "x", Decimal("9.000000000000"), "dup"), (2, "b", Decimal("1.080000000001"), "entry")],
        "id bigint, v string, rate decimal(19,12), kind string",
    )
    (
        delta_table(spark, resolver, T)
        .alias("tgt")
        .merge(src.alias("src"), F.col("tgt.id") == F.col("src.id"))
        .whenNotMatchedInsertAll()
        .execute()
    )
    table = read_table(spark, resolver, T)
    assert [(f.name, f.dataType.simpleString(), f.nullable) for f in table.schema.fields][2:] == [
        ("rate", "decimal(19,12)", True),
        ("kind", "string", True),
    ]
    assert _rows(table, "id") == [(1, "a", None, None), (2, "b", Decimal("1.080000000001"), "entry")]
    with pytest.raises(Exception, match=r"(?i)append.?only"):
        delta_table(spark, resolver, T).delete(F.col("id") == 1)
