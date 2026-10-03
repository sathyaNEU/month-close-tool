"""Smoke job: the end-to-end probe for the whole platform (Stage 0 entrypoints, Stage 1 orchestration).

It follows the same protocol every writer job will follow:

1. claim the writer fence with this attempt's token (first action);
2. check the fence, abort before committing if a newer writer exists;
3. atomically replace this run's partition, recording ``userMetadata`` (run, attempt, token);
4. check the fence again after the commit, and report ``fenced`` if a newer writer appeared;
5. print exactly one ``CLOSE_JOB_RESULT`` line (rows, digest per entity, master, versions).

Rerunning with the same ``--run-id`` replaces that run's partition and yields the same digests.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from close_contracts.resolver import PathTableResolver
from close_contracts.tables import Tables
from close_engine import __version__ as ENGINE_VERSION
from close_engine.faults import pause_point
from close_engine.io.digest import dataframe_digest
from close_engine.io.fence import Fence, FencedError
from close_engine.io.manifest import write_manifest
from close_engine.io.tables import read_table, save
from close_engine.jobs import RESULT_PREFIX
from close_engine.runctx import RunContext, add_contract_args, context_from_args
from close_engine.session import get_session
from close_engine.sparkfns.ids import hash_id
from close_engine.sparkfns.money import convert_minor

EUR_PER_USD = "0.923456789012"
ENTITIES = ("US01", "EU01")
EXIT_FENCED = 3


def build_rows(spark: SparkSession, run_id: str, rows: int) -> DataFrame:
    seq = F.col("id")
    amount = (seq * 7919) % 1_000_000 - 500_000
    return spark.range(0, rows, 1, numPartitions=4).select(
        F.lit(run_id).alias("run_id"),
        seq.alias("seq"),
        hash_id("smk", F.lit(run_id), seq).alias("row_id"),
        F.element_at(F.array(*[F.lit(e) for e in ENTITIES]), (seq % len(ENTITIES) + 1).cast("int")).alias("entity_id"),
        F.element_at(F.array(F.lit("USD"), F.lit("EUR"), F.lit("GBP")), (seq % 3 + 1).cast("int")).alias("currency"),
        amount.alias("amount_minor"),
        convert_minor(amount, F.lit(EUR_PER_USD), 0).alias("amount_eur_minor"),
    )


def _parse(argv: Sequence[str] | None) -> tuple[RunContext, int, Path | None]:
    parser = argparse.ArgumentParser(prog="close-job smoke", description=__doc__)
    add_contract_args(parser)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--result-file", type=Path, default=None, help="also write the JSON result here")
    args = parser.parse_args(argv)
    if not 0 < args.rows <= 10_000_000:
        parser.error("--rows must be between 1 and 10,000,000")
    return context_from_args(parser, args), args.rows, args.result_file


def _emit(result: dict[str, object], result_file: Path | None) -> None:
    line = json.dumps(result, sort_keys=True)
    sys.stdout.write(f"{RESULT_PREFIX}{line}\n")
    sys.stdout.flush()
    if result_file is not None:
        result_file.write_text(line + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    ctx, rows, result_file = _parse(argv)
    spark = get_session("close-smoke")
    resolver = PathTableResolver(lake_root=ctx.root_uri)
    target = Tables.OPS_SMOKE_RESULTS
    fence = Fence(spark, resolver, ctx, job="smoke")
    base: dict[str, object] = {
        "job": "smoke",
        "run_id": ctx.run_id,
        "attempt": ctx.attempt,
        "fencing_token": ctx.fencing_token,
        "table": resolver.table(target),
        "engine_version": ENGINE_VERSION,
        "spark_master": str(spark.conf.get("spark.master", "unknown")),
    }

    fence.claim()
    pause_point("after_claim")
    try:
        fence.check("before_commit")
    except FencedError as err:
        _emit({**base, "fenced": True, "fenced_stage": err.stage, "highest_token": err.highest, "rows": 0}, result_file)
        return EXIT_FENCED
    pause_point("before_commit")

    writer = (
        build_rows(spark, ctx.run_id, rows)
        .write.format("delta")
        .mode("overwrite")
        # Atomic replace of this run's partition. run_id is validated by the run context, so the
        # predicate can only ever be a single-quoted identifier-safe literal.
        .option("replaceWhere", f"run_id = '{ctx.run_id}'")
        .option("userMetadata", ctx.commit_metadata())
        .partitionBy("run_id")
    )
    save(writer, resolver, target)

    written = read_table(spark, resolver, target).where(F.col("run_id") == ctx.run_id)
    by_entity = {entity: written.where(F.col("entity_id") == F.lit(entity)) for entity in ENTITIES}
    result: dict[str, object] = {
        **base,
        "rows": written.count(),
        "digest": dataframe_digest(written),
        "digests_by_entity": {entity: dataframe_digest(df) for entity, df in by_entity.items()},
        "rows_by_entity": {entity: df.count() for entity, df in by_entity.items()},
        "fenced": False,
    }
    try:
        fence.check("before_manifest")  # every commit is preceded by a check (ADR-0007)
    except FencedError as err:
        result.update({"fenced": True, "fenced_stage": err.stage, "highest_token": err.highest})
        _emit(result, result_file)
        return EXIT_FENCED
    write_manifest(
        spark,
        resolver,
        ctx,
        job="smoke",
        rows_by_entity=result["rows_by_entity"],  # type: ignore[arg-type]
        digests_by_entity=result["digests_by_entity"],  # type: ignore[arg-type]
    )
    try:
        fence.check("after_commit")
    except FencedError as err:
        result.update({"fenced": True, "fenced_stage": err.stage, "highest_token": err.highest})
        _emit(result, result_file)
        return EXIT_FENCED
    _emit(result, result_file)
    return 0 if result["rows"] == rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
