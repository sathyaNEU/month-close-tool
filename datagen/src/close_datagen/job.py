"""The datagen job: ``close-job datagen --profile small --seed 11 [contract args]``.

It makes sure the lake is migrated, generates the profile in memory, replaces the landing and reference
tables, appends the run record, answer key, break log and manifest (with a digest per landing table), and
checks the landing control totals Spark reads back against the ones the generator computed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from close_contracts.canonical_json import content_sha256
from close_contracts.resolver import PathTableResolver, TableResolver
from close_contracts.table_schemas import REGISTRY, TableSchema
from close_datagen import __version__
from close_datagen.controls import accumulate, actual, running
from close_datagen.generate import break_counts, generate
from close_datagen.profiles import PROFILES
from close_datagen.writer import insert_missing, write
from close_engine.io.digest import dataframe_digest
from close_engine.io.tables import read_table
from close_engine.jobs import RESULT_PREFIX
from close_engine.jobs.ddl_apply import apply_all
from close_engine.runctx import RunContext, add_contract_args, context_from_args
from close_engine.session import get_session

EXIT_MIGRATIONS: Final = 3
EXIT_TOTALS: Final = 4


def schema(key: str) -> TableSchema:
    return next(s for t, s in REGISTRY.items() if t.key == key)


def digests(spark: object, resolver: TableResolver, keys: list[str]) -> dict[str, str]:
    return {key: dataframe_digest(read_table(spark, resolver, schema(key).table)) for key in keys}  # type: ignore[arg-type]


def store(spark: object, resolver: TableResolver, key: str, rows: list[dict[str, object]], meta: str) -> None:
    """Replace a data table, or insert only missing rows into append-only reference data."""
    if schema(key).append_only:
        insert_missing(spark, resolver, schema(key), rows)  # type: ignore[arg-type]
    else:
        write(spark, resolver, schema(key), rows, metadata=meta)  # type: ignore[arg-type]


def append(spark: object, resolver: TableResolver, key: str, rows: list[dict[str, object]], meta: str) -> None:
    if rows:
        write(spark, resolver, schema(key), rows, metadata=meta, append=True)  # type: ignore[arg-type]


def run(ctx: RunContext, profile_name: str, seed: int) -> dict[str, object]:
    spark = get_session("close-datagen")
    resolver = PathTableResolver(lake_root=ctx.root_uri)
    started = time.monotonic()
    migrated = apply_all(spark, resolver, ctx)
    if not migrated["ok"]:
        return {"ok": False, "exit": EXIT_MIGRATIONS, "migrations": migrated}
    profile = PROFILES[profile_name]
    meta = ctx.commit_metadata()
    totals = running()

    started_tables: set[str] = set()

    def sink(key: str, rows: list[dict[str, object]]) -> None:
        if key.startswith("ops."):
            append(spark, resolver, key, rows, meta)  # the answer key, streamed out while generating
            return
        accumulate(totals, key, rows)
        if key in started_tables:
            append(spark, resolver, key, rows, meta)  # a later batch of a table already replaced in this run
        else:
            store(spark, resolver, key, rows, meta)
            started_tables.add(key)

    built = generate(seed, profile, ctx.run_id, sink)
    built.flush(*sorted(k for k in built.rows if k.startswith(("landing.", "ref."))))  # the reference tables
    generated_s = time.monotonic() - started
    written = sorted(k for k in built.written if k.startswith(("landing.", "ref.")))
    landing = [k for k in written if k.startswith("landing.")]
    wanted = {k: (v[0], v[1]) for k, v in totals.items()}
    got = actual(spark, resolver, landing)
    table_digests = digests(spark, resolver, landing)
    config_hash = content_sha256({"profile": profile_name, "seed": seed, "generator": __version__})
    run_row = {
        "run_id": ctx.run_id,
        "profile": profile_name,
        "seed": seed,
        "config_hash": config_hash,
        "start_date": profile.start,
        "days": profile.days,
        "generator_version": __version__,
        "generated_at": ctx.requested_at,
    }
    manifest = [
        {
            "run_id": ctx.run_id,
            "table_key": table,
            "currency": currency,
            "row_count": rows,
            "amount_total_minor": amount,
            "digest": table_digests[table],
        }
        for (table, currency), (rows, amount) in sorted(wanted.items())
    ]
    append(spark, resolver, "ops.datagen_runs", [run_row], meta)
    append(spark, resolver, "ops.datagen_manifest", manifest, meta)
    append(spark, resolver, "ops.injected_breaks", built.rows.get("ops.injected_breaks", []), meta)
    built.flush_answers()
    mismatched = sorted(f"{t}/{c}" for t, c in set(wanted) | set(got) if wanted.get((t, c)) != got.get((t, c)))
    return {
        "ok": not mismatched,
        "exit": 0 if not mismatched else EXIT_TOTALS,
        "profile": profile_name,
        "seed": seed,
        "rows": {k: built.count(k) for k in written},
        "landing_rows": sum(built.count(k) for k in landing),
        "breaks": break_counts(built),
        "answer_key_groups": built.answer_total[0],
        "totals_mismatched": mismatched,
        "digests": table_digests,
        "generate_seconds": round(generated_s, 1),
        "seconds": round(time.monotonic() - started, 1),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="close-job datagen", description=__doc__)
    add_contract_args(parser)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="small")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--result-file", type=Path, default=None)
    args = parser.parse_args(argv)
    ctx = context_from_args(parser, args)
    result = {"job": "datagen", "run_id": ctx.run_id, **run(ctx, args.profile, args.seed)}
    line = json.dumps(result, sort_keys=True, default=str)
    sys.stdout.write(f"{RESULT_PREFIX}{line}\n")
    sys.stdout.flush()
    if args.result_file is not None:
        args.result_file.write_text(line + "\n", encoding="utf-8")
    return int(str(result["exit"]))
