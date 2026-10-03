"""Writer fencing in jobs (Gate 1a "zombie" criterion) with deterministic interleavings.

Each "job" here is the real smoke entrypoint with pause points, driven in-process so the interleaving
is exact. The residual check-to-commit window is pinned by a strict xfail (see ADR S5')."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from close_contracts.resolver import PathTableResolver
from close_contracts.tables import Tables
from close_engine.io.digest import dataframe_digest
from close_engine.io.tables import delta_table, read_table
from close_engine.jobs import RESULT_PREFIX, smoke

pytestmark = pytest.mark.spark

RUN_A = "a" * 32
RUN_B = "b" * 32


def _args(root: Path, run_id: str, token: int, attempt: int = 1) -> list[str]:
    return [
        "--run-id",
        run_id,
        "--attempt",
        str(attempt),
        "--lease-resource",
        "smoke:default",
        "--fencing-token",
        str(token),
        "--requested-at",
        "2026-09-27T12:00:00+00:00",
        "--root-uri",
        root.as_uri(),
        "--rows",
        "200",
    ]


def _result(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(RESULT_PREFIX)]
    assert len(lines) == 1
    parsed: dict[str, object] = json.loads(lines[-1][len(RESULT_PREFIX) :])
    return parsed


def _table_digest(spark: SparkSession, root: Path) -> str:
    return dataframe_digest(read_table(spark, PathTableResolver(lake_root=root.as_uri()), Tables.OPS_SMOKE_RESULTS))


def test_highest_token_writes_and_records_commit_metadata(
    spark: SparkSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert smoke.main(_args(tmp_path, RUN_A, 5)) == 0
    result = _result(capsys)
    assert result["fenced"] is False
    assert result["rows"] == 200
    digests = result["digests_by_entity"]
    assert isinstance(digests, dict)
    assert set(digests) == {"US01", "EU01"}
    history = delta_table(spark, PathTableResolver(lake_root=tmp_path.as_uri()), Tables.OPS_SMOKE_RESULTS).history(1)
    meta = json.loads(history.collect()[0]["userMetadata"])
    assert (meta["run_id"], meta["attempt"], meta["fencing_token"]) == (RUN_A, 1, 5)


def test_stale_token_aborts_before_commit_and_leaves_data_unchanged(
    spark: SparkSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert smoke.main(_args(tmp_path, RUN_B, 6)) == 0  # the newer writer already committed
    capsys.readouterr()
    before = _table_digest(spark, tmp_path)
    assert smoke.main(_args(tmp_path, RUN_A, 5)) == smoke.EXIT_FENCED  # the zombie wakes up late
    result = _result(capsys)
    assert (result["fenced"], result["fenced_stage"], result["highest_token"]) == (True, "before_commit", 6)
    assert _table_digest(spark, tmp_path) == before
    written = read_table(spark, PathTableResolver(lake_root=tmp_path.as_uri()), Tables.OPS_SMOKE_RESULTS)
    assert written.where(F.col("run_id") == RUN_A).count() == 0


def _run_paused(args: list[str], release: Path, out: dict[str, int]) -> threading.Thread:
    def target() -> None:
        out["rc"] = smoke.main(args)

    thread = threading.Thread(target=target)
    thread.start()
    deadline = time.monotonic() + 120
    while not Path(f"{release}.reached").exists():
        assert time.monotonic() < deadline, "zombie never reached its pause point"
        time.sleep(0.05)
    return thread


def test_newer_claim_while_zombie_waits_is_caught_before_commit(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release-after-claim"
    monkeypatch.setenv("CLOSE_FAULTS", f"after_claim={release}")
    out: dict[str, int] = {}
    zombie = _run_paused(_args(tmp_path, RUN_A, 5), release, out)  # zombie claimed token 5, then paused
    monkeypatch.delenv("CLOSE_FAULTS")
    os.environ.pop("CLOSE_FAULTS", None)
    assert smoke.main(_args(tmp_path, RUN_B, 6)) == 0  # newer writer claims 6 and commits
    before = _table_digest(spark, tmp_path)
    release.touch()
    zombie.join(timeout=120)
    assert out["rc"] == smoke.EXIT_FENCED
    assert _table_digest(spark, tmp_path) == before


@pytest.mark.xfail(strict=True, reason="residual window: a zombie that passed its check can still commit (ADR S5')")
def test_residual_window_between_check_and_commit(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release-before-commit"
    monkeypatch.setenv("CLOSE_FAULTS", f"before_commit={release}")
    out: dict[str, int] = {}
    zombie = _run_paused(_args(tmp_path, RUN_A, 5), release, out)  # passed its check, paused before commit
    monkeypatch.delenv("CLOSE_FAULTS")
    os.environ.pop("CLOSE_FAULTS", None)
    assert smoke.main(_args(tmp_path, RUN_B, 6)) == 0
    before = _table_digest(spark, tmp_path)
    release.touch()
    zombie.join(timeout=120)
    # The zombie does detect it afterwards (fenced after commit)...
    assert out["rc"] == smoke.EXIT_FENCED
    # ...but its data already landed, which is exactly the documented residual window.
    assert _table_digest(spark, tmp_path) == before


def test_the_run_manifest_records_each_entity_with_the_writers_token(
    spark: SparkSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from close_contracts.queryable import RUN_MANIFESTS

    assert smoke.main(_args(tmp_path, RUN_A, 5)) == 0
    result = _result(capsys)
    manifest = read_table(spark, PathTableResolver(lake_root=tmp_path.as_uri()), Tables.OPS_RUN_MANIFESTS)
    assert [f.name for f in manifest.schema.fields] == [c.name for c in RUN_MANIFESTS.columns]
    rows = sorted((r["entity_id"], r["row_count"], r["digest"], r["fencing_token"]) for r in manifest.collect())
    assert rows == sorted(
        (e, result["rows_by_entity"][e], d, 5)  # type: ignore[index]
        for e, d in result["digests_by_entity"].items()  # type: ignore[union-attr]
    )


def test_smoke_results_match_the_queryable_definition(spark: SparkSession, tmp_path: Path) -> None:
    from close_contracts.queryable import SMOKE_RESULTS

    assert smoke.main(_args(tmp_path, RUN_B, 7)) == 0
    table = read_table(spark, PathTableResolver(lake_root=tmp_path.as_uri()), Tables.OPS_SMOKE_RESULTS)
    types = {"bigint": "bigint", "string": "string"}
    assert [(f.name, f.dataType.simpleString()) for f in table.schema.fields] == [
        (c.name, types[c.type]) for c in SMOKE_RESULTS.columns
    ]
