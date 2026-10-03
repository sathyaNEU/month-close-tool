"""Spike S1 (Stage 1b): the engine's Unity Catalog code paths against Unity Catalog OSS 0.6.0.

Runs in its own JVM (the UC connector must be on the classpath from the start) and prints one JSON
document: for each operation, whether it worked and what happened. ``engine/tests/uc/test_uc_spike.py``
runs it and records the outcome; ADR-0010 interprets it.

    python uc_spike.py <uc-uri> <work-dir>
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from delta import configure_spark_with_delta_pip
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from close_contracts.query import Eq, QuerySpec, Sort
from close_contracts.queryable import SMOKE_RESULTS
from close_contracts.resolver import CatalogTableResolver
from close_contracts.tables import Tables
from close_engine.io.tables import current_version, delta_table, read_table, save
from close_platform.query.dataframe import DataFrameBackend
from close_platform.query.engine import QueryPlan
from close_platform.query.history import latest_version

UC_CONNECTOR = "io.unitycatalog:unitycatalog-spark_4.1_2.13:0.6.0"
CATALOG = "unity"


def main(uri: str, work: str) -> int:
    builder = (
        SparkSession.builder.master("local[2]")
        .appName("uc-spike")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "io.unitycatalog.spark.UCSingleCatalog")
        .config(f"spark.sql.catalog.{CATALOG}", "io.unitycatalog.spark.UCSingleCatalog")
        .config(f"spark.sql.catalog.{CATALOG}.uri", uri)
        .config(f"spark.sql.catalog.{CATALOG}.token", "")
        .config("spark.sql.defaultCatalog", CATALOG)
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.ansi.enabled", "true")
    )
    spark = configure_spark_with_delta_pip(builder, extra_packages=[UC_CONNECTOR]).getOrCreate()
    resolver = CatalogTableResolver(catalog=CATALOG, evidence_root=Path(work).as_uri())
    t = Tables.OPS_SMOKE_RESULTS
    fqn = resolver.table(t)
    rows = spark.range(0, 20).select(
        F.lit("r" * 32).alias("run_id"),
        F.col("id").alias("seq"),
        F.sha2(F.col("id").cast("string"), 256).substr(1, 32).alias("row_id"),
        F.when(F.col("id") % 2 == 0, F.lit("US01")).otherwise(F.lit("EU01")).alias("entity_id"),
        F.lit("USD").alias("currency"),
        F.col("id").alias("amount_minor"),
        F.col("id").alias("amount_eur_minor"),
    )
    results: dict[str, dict[str, Any]] = {}

    def step(name: str, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
            results[name] = {"ok": True, "detail": value}
            return value
        except Exception as exc:  # the spike records every failure instead of stopping at the first
            results[name] = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:600]}"}
            traceback.print_exc(file=sys.stderr)
            return None

    step("create_schema", lambda: spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.ops") and "ok")
    step("save_managed_saveAsTable", lambda: save(rows.write.format("delta").mode("overwrite"), resolver, t) or "ok")
    if not results["save_managed_saveAsTable"]["ok"]:
        external = Path(work, "external", "smoke_results").as_uri()
        step(
            "save_external_saveAsTable",
            lambda: (
                rows.write.format("delta").mode("errorifexists").option("path", external).saveAsTable(fqn) or external
            ),
        )
    step("append_second_version", lambda: rows.limit(5).write.format("delta").mode("append").saveAsTable(fqn) or "ok")
    step("deltatable_forName_history", lambda: delta_table(spark, resolver, t).history().count())
    step("current_version", lambda: current_version(spark, resolver, t))
    step("time_travel_via_table_v0", lambda: read_table(spark, resolver, t, version=0).count())
    step("latest_version_describe_history", lambda: latest_version(spark, resolver, t))
    spec = QuerySpec(
        SMOKE_RESULTS.key, ("row_id", "entity_id"), (Eq("run_id", "r" * 32),), (Sort("row_id"),), limit=100
    )
    backend = DataFrameBackend(lambda work_fn: work_fn(spark), resolver)
    step(
        "read_path_backend_v0",
        lambda: len(backend.fetch(QueryPlan(spec, SMOKE_RESULTS, frozenset({"US01"}), None, 0, 101))),
    )
    print(json.dumps(results, sort_keys=True))
    spark.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
