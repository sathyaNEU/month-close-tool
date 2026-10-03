"""The journaling stand-in (Stage 8a, ADR-0019): the reference engine's journals for one entity, written to Delta.

The Spark journaling job waits for the rule compiler. Until then this job runs the pure-Python reference engine, the
one Stage 6 holds to its oracle and invariants, so the Databricks GL has the engine's journals on sample data. It is
for sample volumes only: it reads its inputs into Python.

1. **Claim** the writer fence with this attempt's token (first action).
2. **Read the snapshot** the run was started with, as ``close_ledger_reader`` through ``job_config_snapshots``, and
   recompute its SHA-256: a mismatch is refused. It holds the active journaling configuration and entity set, and
   the entity's period statuses in Postgres (the ``ref.periods`` the generator writes says every period is open).
3. **Read the inputs**: the landing tables the event types' sources and lookups name, and ``ref`` currencies,
   entities, daily rates and calendars. Their digest must equal the run's pinned digest when it has one, and the
   entity set's functional currencies must equal ``ref.entities``: either difference is refused before writing.
4. **Journal** them with the reference engine, keep the entity's lines, and roll them up.
5. **Compare** with what the entity's ledger already holds. Every row there must be one this run would write, as it
   is. Otherwise an earlier run wrote it from other inputs or another configuration (one that failed, so never
   pinned), or under other period statuses (the pins don't cover a period closed since), and an insert-only write
   would leave it standing next to this run's rows. That is refused.
6. **Write** the subledger lines, then the rollups, each insert-only on the engine's deterministic ids, scoped to
   the entity's partition. A crash between the two is completed by the retry, and a rerun writes nothing.
7. **Check** the fence again, and print one ``CLOSE_JOB_RESULT`` line.

A refusal is a result, not a failure: the run ends with ``refused`` and the reason, and nothing is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from close_contracts.calendar import BusinessCalendar
from close_contracts.canonical_json import content_sha256
from close_contracts.config.accounting_policy import AccountingPolicy
from close_contracts.config.journal_workflow import EntitySet
from close_contracts.config.journaling import (
    AccountMapping,
    AutoJournalDefinition,
    ChartOfAccounts,
    DimensionSet,
    EventType,
)
from close_contracts.resolver import PathTableResolver, TableResolver
from close_contracts.table_schemas import ALL_TABLES, REGISTRY
from close_contracts.tables import LogicalTable, Tables
from close_engine import __version__ as ENGINE_VERSION
from close_engine.faults import pause_point
from close_engine.io.digest import dataframe_digest
from close_engine.io.fence import Fence, FencedError
from close_engine.io.rows import insert_missing
from close_engine.io.tables import delta_table, read_table
from close_engine.jobs import RESULT_PREFIX
from close_engine.reference.journal.ledger import RollupLine, rollup
from close_engine.reference.journal.posting import JournalConfig, Line
from close_engine.reference.journal.runner import journal, reference_from
from close_engine.runctx import RunContext, add_contract_args, context_from_args
from close_engine.session import get_session

EXIT_FENCED: Final = 3
SUBLEDGER: Final = REGISTRY[Tables.LEDGER_SUBLEDGER_LINES]
ROLLUPS: Final = REGISTRY[Tables.LEDGER_GL_ROLLUP_LINES]
BY_KEY: Final = {t.table.key: t.table for t in ALL_TABLES}
REF_INPUTS: Final = ("ref.currencies", "ref.entities", "ref.fx_rates", "ref.calendars")
SNAPSHOT_SQL: Final = "SELECT content FROM job_config_snapshots WHERE snapshot_hash = %s"
CLOSED: Final = frozenset({"soft_closed", "closed", "locked"})  # take no automated entries (Stage 8b, ADR-0020)
STAMPED: Final = frozenset({"run_id", "created_at"})  # what the writing run stamps, not what the engine books


class RefusedError(Exception):
    """A run that must not write: its snapshot, inputs or entities don't match what it was started with."""


def read_snapshot(snapshot_hash: str) -> dict[str, Any]:
    """The run's snapshot, as ``close_ledger_reader`` (libpq ``PG*`` variables in the jobs service)."""
    import psycopg

    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute(SNAPSHOT_SQL, (snapshot_hash,))
        found = cur.fetchone()
    if found is None:
        raise RefusedError(f"snapshot {snapshot_hash} is not visible (is its run still in progress?)")
    content: dict[str, Any] = found[0]
    return content


def config_from(snapshot: Mapping[str, Any]) -> tuple[JournalConfig, dict[str, int], dict[str, str]]:
    """The engine's configuration, each definition's version, and the entity set's functional currencies."""
    kinds: Mapping[str, list[dict[str, Any]]] = snapshot["configuration"]

    def specs(kind: str) -> list[dict[str, Any]]:
        return [v["spec"] for v in kinds.get(kind, [])]

    def one(kind: str) -> dict[str, Any]:
        found = specs(kind)
        if len(found) != 1:
            raise RefusedError(f"the snapshot has {len(found)} active {kind} versions, not one")
        return found[0]

    config = JournalConfig(
        policy=AccountingPolicy.model_validate(one("accounting_policy")),
        chart=ChartOfAccounts.model_validate(one("chart_of_accounts")),
        dimensions=DimensionSet.model_validate(one("dimension_set")),
        mappings={m.mapping_id: m for m in map(AccountMapping.model_validate, specs("account_mapping"))},
        event_types={e.event_type: e for e in map(EventType.model_validate, specs("event_type"))},
        definitions=[AutoJournalDefinition.model_validate(d) for d in specs("auto_journal_definition")],
    )
    versions = {v["spec"]["definition_id"]: int(v["version"]) for v in kinds.get("auto_journal_definition", [])}
    entities = EntitySet.model_validate(one("entity_set"))
    return config, versions, {e.entity_id: e.functional_currency for e in entities.entities}


def input_tables(config: JournalConfig) -> list[str]:
    """The landing tables the event types read (their sources and lookups), and the reference tables."""
    landing = {s.landing_table for e in config.event_types.values() for s in e.sources}
    landing |= {lk.table for e in config.event_types.values() for s in e.sources for lk in s.lookups}
    return sorted(landing) + list(REF_INPUTS)


def _frame(spark: SparkSession, resolver: TableResolver, key: str) -> DataFrame:
    table = BY_KEY.get(key)
    if table is None:
        raise RefusedError(f"{key} is not a registered table")
    return read_table(spark, resolver, table)


def inputs_digest(digests: Mapping[str, str]) -> str:
    """One digest over every input table's content digest, independent of the order they were read in."""
    joined = "\n".join(f"{key}={digest}" for key, digest in sorted(digests.items()))
    return hashlib.sha256(joined.encode()).hexdigest()


def calendars_from(rows: Iterable[Mapping[str, Any]]) -> dict[str, BusinessCalendar]:
    """Business calendars rebuilt from ``ref.calendars``: a weekday that isn't a business day is a holiday."""
    holidays: dict[str, set[date]] = {}
    for r in rows:
        days = holidays.setdefault(str(r["calendar_code"]), set())
        if not r["is_business_day"] and r["day"].weekday() < 5:
            days.add(r["day"])
    return {code: BusinessCalendar(code, frozenset(days)) for code, days in holidays.items()}


def subledger_rows(
    lines: Sequence[Line], links: Mapping[str, str], versions: Mapping[str, int], run_id: str, at: datetime
) -> list[dict[str, Any]]:
    return [
        {
            "line_id": ln.line_id,
            "event_id": ln.event_id,
            "event_version": ln.event_version,
            "posting_rule_id": ln.definition_id or ln.template,  # suspense and rounding lines have no definition
            "posting_rule_version": versions.get(ln.definition_id, 0),
            "entity_id": ln.entity_id,
            "accounting_date": ln.accounting_date,
            "period": ln.period,
            "account": ln.account,
            "side": ln.side,
            "amount_minor": ln.amount_minor,
            "currency": ln.currency,
            "functional_amount_minor": ln.functional_amount_minor,
            "functional_currency": ln.functional_currency,
            "fx_rate": ln.fx_rate,
            "fx_rate_type": "daily_spot" if ln.fx_rate is not None else None,
            "fx_rate_date": ln.fx_rate_date,
            "fx_rate_version": ln.fx_rate_version,
            "dimensions": dict(ln.dimensions),
            "description": ln.description,
            "rollup_line_id": links[ln.line_id],
            "is_suspense": ln.is_suspense,
            "intended_account": ln.intended_account,
            "is_prior_period_adjustment": ln.is_prior_period_adjustment,
            "original_period": ln.original_period,
            "run_id": run_id,
            "created_at": at,
        }
        for ln in lines
    ]


def rollup_rows(
    rollups: Sequence[RollupLine], lines: Sequence[Line], links: Mapping[str, str], run_id: str, at: datetime
) -> list[dict[str, Any]]:
    dimensions = {links[ln.line_id]: dict(ln.dimensions) for ln in lines}
    return [
        {
            "rollup_line_id": r.rollup_line_id,
            "entity_id": r.entity_id,
            "period": r.period,
            "accounting_date": None,  # period rollups
            "account": r.account,
            "dimensions_key": r.dimensions_key,
            "dimensions": dimensions[r.rollup_line_id],
            "currency": r.currency,
            "debit_minor": r.debit_minor,
            "credit_minor": r.credit_minor,
            "line_count": r.line_count,
            "run_id": run_id,
            "created_at": at,
            "functional_debit_minor": r.functional_debit_minor,
            "functional_credit_minor": r.functional_credit_minor,
        }
        for r in rollups
    ]


def _as_written(value: Any) -> Any:
    return dict(value) if isinstance(value, list) else value  # Arrow returns a map as a list of pairs


def disagreements(existing: Iterable[Mapping[str, Any]], computed: Sequence[Mapping[str, Any]], key: str) -> list[str]:
    """The ids of rows already in the ledger that this run wouldn't write as they are."""
    ours = {r[key]: r for r in computed}
    out = []
    for row in existing:
        mine = ours.get(row[key])
        if mine is None or any(_as_written(row[c]) != _as_written(v) for c, v in mine.items() if c not in STAMPED):
            out.append(str(row[key]))
    return sorted(out)


def _parse(argv: Sequence[str] | None) -> tuple[RunContext, str, str, str, Path | None]:
    parser = argparse.ArgumentParser(prog="close-job journal_ref", description=__doc__)
    add_contract_args(parser)
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--snapshot-hash", required=True)
    parser.add_argument("--pinned-inputs", default="", help="the digest the first successful run pinned, if any")
    parser.add_argument("--result-file", type=Path, default=None, help="also write the JSON result here")
    args = parser.parse_args(argv)
    return context_from_args(parser, args), args.entity_id, args.snapshot_hash, args.pinned_inputs, args.result_file


def _emit(result: dict[str, object], result_file: Path | None) -> None:
    line = json.dumps(result, sort_keys=True, default=str)
    sys.stdout.write(f"{RESULT_PREFIX}{line}\n")
    sys.stdout.flush()
    if result_file is not None:
        result_file.write_text(line + "\n", encoding="utf-8")


def _in_the_ledger(
    spark: SparkSession, resolver: TableResolver, entity_id: str, line_rows: list[Any], summed: list[Any]
) -> tuple[int, int]:
    """How many lines and rollups the entity's ledger holds, all of them rows this run would write as they are."""
    present: list[list[dict[str, Any]]] = [
        read_table(spark, resolver, t.table).where(F.col("entity_id") == F.lit(entity_id)).toArrow().to_pylist()
        for t in (SUBLEDGER, ROLLUPS)
    ]
    stale = disagreements(present[0], line_rows, "line_id") + disagreements(present[1], summed, "rollup_line_id")
    if stale:
        raise RefusedError(
            f"the ledger holds {len(stale)} rows this run wouldn't write as they are, from an earlier run with other "
            f"inputs, configuration or period statuses (first: {stale[0]})"
        )
    return len(present[0]), len(present[1])


def _count(spark: SparkSession, resolver: TableResolver, table: LogicalTable, entity_id: str) -> int:
    return int(read_table(spark, resolver, table).where(F.col("entity_id") == F.lit(entity_id)).count())


def _prepare(
    spark: SparkSession, resolver: TableResolver, snapshot_hash: str, pinned: str, entity_id: str
) -> tuple[dict[str, Any], JournalConfig, dict[str, int], dict[str, list[dict[str, Any]]], str]:
    """The verified snapshot, the engine's configuration and inputs, and the inputs' digest; or a refusal."""
    snapshot = read_snapshot(snapshot_hash)
    if content_sha256(snapshot) != snapshot_hash:
        raise RefusedError("the snapshot's content doesn't hash to the run's snapshot hash")
    config, versions, functional = config_from(snapshot)
    frames = {key: _frame(spark, resolver, key) for key in input_tables(config)}
    digest = inputs_digest({key: dataframe_digest(df) for key, df in frames.items()})
    if pinned and digest != pinned:
        raise RefusedError(f"the inputs changed since the ledger was first written ({pinned[:12]}, now {digest[:12]})")
    tables = {key: df.toArrow().to_pylist() for key, df in frames.items()}
    lake = {str(r["entity_id"]): str(r["functional_currency"]) for r in tables["ref.entities"]}
    mismatched = sorted(e for e in functional if e in lake and lake[e] != functional[e])
    if mismatched or entity_id not in functional:
        raise RefusedError(
            f"the entity set and ref.entities disagree on functional currencies: {mismatched or [entity_id]}"
        )
    return snapshot, config, versions, tables, digest


def main(argv: Sequence[str] | None = None) -> int:
    ctx, entity_id, snapshot_hash, pinned, result_file = _parse(argv)
    spark = get_session("close-journal-reference")
    resolver = PathTableResolver(lake_root=ctx.root_uri)
    fence = Fence(spark, resolver, ctx, job="journal_ref")
    base: dict[str, object] = {
        "job": "journal_ref",
        "run_id": ctx.run_id,
        "attempt": ctx.attempt,
        "fencing_token": ctx.fencing_token,
        "entity_id": entity_id,
        "snapshot_hash": snapshot_hash,
        "engine_version": ENGINE_VERSION,
        "fenced": False,
    }
    fence.claim()
    pause_point("after_claim")
    try:
        snapshot, config, versions, tables, digest = _prepare(spark, resolver, snapshot_hash, pinned, entity_id)
    except RefusedError as refused:
        _emit({**base, "refused": str(refused)}, result_file)
        return 0
    closed = frozenset(p for p, status in snapshot["periods"].items() if status in CLOSED)
    ref = reference_from(tables, closed={entity_id: closed})
    journaled = journal(tables, config, calendars=calendars_from(tables["ref.calendars"]), ref=ref)
    if journaled.result.failed_types:
        _emit({**base, "failed_types": journaled.result.failed_types}, result_file)
        return 1
    lines = [ln for ln in journaled.result.lines if ln.entity_id == entity_id]
    rollups, links = rollup(lines)
    at = ctx.requested_at.astimezone(UTC)  # the run's clock: jobs never read the wall clock
    line_rows = subledger_rows(lines, links, versions, ctx.run_id, at)
    summed = rollup_rows(rollups, lines, links, ctx.run_id, at)
    try:
        before = _in_the_ledger(spark, resolver, entity_id, line_rows, summed)
    except RefusedError as refused:
        _emit({**base, "refused": str(refused)}, result_file)
        return 0
    try:
        fence.check("before_commit")
    except FencedError as err:
        _emit({**base, "fenced": True, "fenced_stage": err.stage, "highest_token": err.highest}, result_file)
        return EXIT_FENCED
    pause_point("before_commit")
    metadata = ctx.commit_metadata()
    within = F.col("t.entity_id") == F.lit(entity_id)
    if lines:
        insert_missing(spark, resolver, SUBLEDGER, line_rows, within=within, metadata=metadata)
    pause_point("after_lines_commit")
    try:
        fence.check("before_rollups")  # every commit is preceded by a check (ADR-0007)
    except FencedError as err:
        _emit({**base, "fenced": True, "fenced_stage": err.stage, "highest_token": err.highest}, result_file)
        return EXIT_FENCED
    if rollups:
        insert_missing(spark, resolver, ROLLUPS, summed, within=within, metadata=metadata)
    after = (_count(spark, resolver, SUBLEDGER.table, entity_id), _count(spark, resolver, ROLLUPS.table, entity_id))
    history = delta_table(spark, resolver, Tables.LEDGER_GL_ROLLUP_LINES).history(1).collect()
    result: dict[str, object] = {
        **base,
        "configuration_hash": content_sha256(snapshot["configuration"]),
        "inputs_digest": digest,
        "events": len({ln.event_id for ln in lines}),
        "lines": len(lines),
        "rollups": len(rollups),
        "lines_written": after[0] - before[0],
        "rollups_written": after[1] - before[1],
        "suspense_lines": sum(1 for ln in lines if ln.is_suspense),
        "prior_period_adjustments": sum(1 for ln in lines if ln.is_prior_period_adjustment),
        "commit_version": int(history[0]["version"]),
    }
    try:
        fence.check("after_commit")
    except FencedError as err:
        result.update({"fenced": True, "fenced_stage": err.stage, "highest_token": err.highest})
        _emit(result, result_file)
        return EXIT_FENCED
    _emit(result, result_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
