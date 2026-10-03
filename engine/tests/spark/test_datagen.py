"""The datagen job end to end on the tiny profile: tables, control totals, answer key, determinism."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pyspark.sql import SparkSession

from close_contracts.resolver import PathTableResolver
from close_contracts.table_schemas import LANDING_TABLES, REGISTRY
from close_datagen.job import run
from close_engine.io.tables import read_table
from close_engine.runctx import RunContext

pytestmark = pytest.mark.spark


def _ctx(root: Path, run_id: str) -> RunContext:
    return RunContext(
        run_id=run_id,
        attempt=1,
        lease_resource="datagen:lake",
        fencing_token=1,
        requested_at=datetime(2026, 9, 28, tzinfo=UTC),
        root_uri=root.as_uri(),
        request_id=None,
    )


@pytest.fixture(scope="module")
def tiny(spark: SparkSession, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    root = tmp_path_factory.mktemp("lake-a")
    return root, run(_ctx(root, "dg-a"), "tiny", 11)


def test_the_tiny_profile_writes_every_landing_table_and_the_totals_match(
    spark: SparkSession, tiny: tuple[Path, dict[str, Any]]
) -> None:
    root, result = tiny
    assert result["ok"], result
    assert result["totals_mismatched"] == []
    resolver = PathTableResolver(lake_root=root.as_uri())
    for schema in LANDING_TABLES:
        assert read_table(spark, resolver, schema.table).count() == result["rows"][schema.table.key], schema.table.key


def test_the_answer_key_and_break_log_are_written(spark: SparkSession, tiny: tuple[Path, dict[str, Any]]) -> None:
    root, result = tiny
    resolver = PathTableResolver(lake_root=root.as_uri())
    table = {t.key: t for t in REGISTRY}
    groups = read_table(spark, resolver, table["ops.answer_key_groups"]).where("run_id = 'dg-a'")
    assert groups.count() == result["answer_key_groups"]
    templates = {r["template"] for r in groups.select("template").distinct().collect()}
    assert templates == {1, 2, 3, 6, 9, 10}
    manifest = read_table(spark, resolver, table["ops.datagen_manifest"]).where("run_id = 'dg-a'")
    assert manifest.count() > 0


def test_the_same_seed_gives_the_same_digests_and_another_seed_does_not(
    spark: SparkSession, tiny: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, first = tiny
    again = run(_ctx(tmp_path / "b", "dg-b"), "tiny", 11)
    other = run(_ctx(tmp_path / "c", "dg-c"), "tiny", 23)
    assert again["digests"] == first["digests"]
    changed = [k for k in first["digests"] if other["digests"][k] != first["digests"][k]]
    assert len(changed) >= len(first["digests"]) - 2  # sellers and a few static shapes may coincide
