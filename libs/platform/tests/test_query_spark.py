"""The DataFrame backend on a real local Delta table, held to the reference evaluator, and the SQL
renderer held to both on plain Spark (Gate 1b): the three executors return identical rows."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pyspark.sql import SparkSession
from pyspark.sql.types import LongType, StringType, StructField, StructType
from querykit import ENTITIES, TABLE, dataset, grouped_specs, row_specs

from close_contracts.query import QuerySpec, Row, Sort
from close_contracts.query_reference import evaluate, sort_key
from close_contracts.queryable import RUN_MANIFESTS
from close_contracts.resolver import PathTableResolver
from close_platform.query.dataframe import DataFrameBackend
from close_platform.query.engine import QueryPlan, TableMissingError
from close_platform.query.sql import render

pytestmark = pytest.mark.spark

ROWS = dataset()
SCHEMA = StructType(
    [StructField(c.name, LongType() if c.type == "bigint" else StringType(), c.nullable) for c in TABLE.columns]
)
ARROW = pa.schema([(c.name, pa.int64() if c.type == "bigint" else pa.string()) for c in TABLE.columns])
RUNNER = Path(__file__).with_name("plain_spark_runner.py")
some_entities = st.frozensets(st.sampled_from(ENTITIES[:2]), min_size=1)


@pytest.fixture(scope="module")
def spark() -> SparkSession:
    from close_engine.session import LocalSparkOptions, build_local_session

    active = SparkSession.getActiveSession()  # reuse one if another suite in this process built it
    return active or build_local_session(
        LocalSparkOptions(app_name="close-query-tests", master="local[2]", driver_memory="1g", shuffle_partitions=2)
    )


def _write(spark: SparkSession, rows: list[Row], path: str, mode: str = "overwrite") -> None:
    values = [tuple(r[c.name] for c in TABLE.columns) for r in rows]
    spark.createDataFrame(values, SCHEMA).write.format("delta").mode(mode).save(path)


@pytest.fixture(scope="module")
def lake(spark: SparkSession, tmp_path_factory: pytest.TempPathFactory) -> PathTableResolver:
    root = tmp_path_factory.mktemp("lake")
    resolver = PathTableResolver(lake_root=root.as_uri())
    _write(spark, ROWS, resolver.location(TABLE.table))
    return resolver


@pytest.fixture(scope="module")
def backend(spark: SparkSession, lake: PathTableResolver) -> DataFrameBackend:
    return DataFrameBackend(lambda work: work(spark), lake)


def _after(spec: QuerySpec, draw: st.DrawFn) -> tuple[object, ...] | None:
    if spec.grouped or not draw(st.booleans()):
        return None
    return sort_key(draw(st.sampled_from(ROWS)), spec.sort)


def test_current_version_and_a_missing_table(backend: DataFrameBackend) -> None:
    assert backend.current_version(TABLE) == 0
    with pytest.raises(TableMissingError):
        backend.current_version(RUN_MANIFESTS)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.one_of(row_specs(max_limit=40), grouped_specs()), some_entities, st.data())
def test_dataframe_backend_matches_the_reference(
    backend: DataFrameBackend, spec: QuerySpec, entities: frozenset[str], data: st.DataObject
) -> None:
    after = _after(spec, data.draw)
    plan = QueryPlan(spec, TABLE, entities, after, 0, spec.limit + 1)  # type: ignore[arg-type]
    assert backend.fetch(plan) == evaluate(spec, TABLE, ROWS, entities=entities, after=after, fetch=plan.fetch)  # type: ignore[arg-type]


@pytest.mark.slow
@settings(max_examples=2000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.one_of(row_specs(max_limit=60), grouped_specs()), some_entities)
def test_2000_generated_specs_never_return_rows_outside_the_callers_entities(
    backend: DataFrameBackend, spec: QuerySpec, entities: frozenset[str]
) -> None:
    rows = backend.fetch(QueryPlan(spec, TABLE, entities, None, 0, spec.limit + 1))
    entity_of = {r["row_id"]: r["entity_id"] for r in ROWS}
    if not spec.grouped:
        assert all(entity_of[r["row_id"]] in entities for r in rows)
    elif "entity_id" in spec.group_by:
        assert {r["entity_id"] for r in rows} <= entities
    else:
        assert rows == evaluate(spec, TABLE, ROWS, entities=entities, fetch=spec.limit + 1)


def test_reads_are_pinned_to_the_plan_version(spark: SparkSession, tmp_path: Path) -> None:
    resolver = PathTableResolver(lake_root=tmp_path.as_uri())
    path = resolver.location(TABLE.table)
    _write(spark, ROWS[:50], path)
    _write(spark, ROWS[50:80], path, mode="append")  # version 1
    local = DataFrameBackend(lambda work: work(spark), resolver)
    assert local.current_version(TABLE) == 1
    spec = QuerySpec(TABLE.key, ("row_id",), sort=(Sort("row_id"),), limit=500)
    everyone = frozenset(ENTITIES)
    v0 = local.fetch(QueryPlan(spec, TABLE, everyone, None, 0, 501))
    v1 = local.fetch(QueryPlan(spec, TABLE, everyone, None, 1, 501))
    assert len(v0) == 50
    assert len(v1) == 80


def test_three_executors_agree_on_300_specs(backend: DataFrameBackend, tmp_path: Path) -> None:
    cases: list[tuple[str, dict[str, object], list[Row]]] = []

    @settings(max_examples=300, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow])
    @given(st.one_of(row_specs(max_limit=40), grouped_specs()), some_entities, st.data())
    def collect(spec: QuerySpec, entities: frozenset[str], data: st.DataObject) -> None:
        after = _after(spec, data.draw)
        plan = QueryPlan(spec, TABLE, entities, after, 0, spec.limit + 1)  # type: ignore[arg-type]
        expected = evaluate(spec, TABLE, ROWS, entities=entities, after=after, fetch=plan.fetch)  # type: ignore[arg-type]
        assert backend.fetch(plan) == expected
        rendered = render(dataclasses.replace(plan, version=None), "{table}")  # the Parquet copy has no versions
        cases.append((rendered.text, dict(rendered.params), expected))

    collect()
    assert len(cases) >= 300
    parquet = tmp_path / "smoke.parquet"
    pq.write_table(pa.Table.from_pylist(ROWS, schema=ARROW), parquet)
    jobs, results = tmp_path / "jobs.json", tmp_path / "results.json"
    jobs.write_text(json.dumps({"parquet": str(parquet), "jobs": [{"sql": t, "params": p} for t, p, _ in cases]}))
    env = {**os.environ, "PYSPARK_PYTHON": sys.executable}
    proc = subprocess.run(
        [sys.executable, str(RUNNER), str(jobs), str(results)],
        capture_output=True,
        text=True,
        env=env,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    outcomes = json.loads(results.read_text())
    mismatches = [(t, p, e, got) for (t, p, e), got in zip(cases, outcomes, strict=True) if got != e]
    assert not mismatches, f"{len(mismatches)} of {len(cases)} differ; first: {mismatches[0]}"
