"""Posting job (Stage 7b, ADR-0017): writes the journals frozen into a posting run to the Delta ledger, once.

1. **Claim** the writer fence with this attempt's token (first action).
2. **Read** the run's lines from Postgres through ``posting_queue``, the one view the ``close_ledger_reader`` role
   may read (credentials only in the jobs service).
3. **Find what is already there.** A retry after a commit whose result was lost finds its lines in the ledger.
4. **Write the lines** in one insert-only MERGE into ``ledger.journal_lines``, scoped to the entity's partition,
   after checking the fence. This table is the ledger; nothing reads the feed below as one.
5. **Write the ERP feed:** one row per journal into ``ledger.journal_postings``. A crash between the two commits
   only delays the ERP, and the rerun completes it.
6. **Check** the fence again, and print one ``CLOSE_JOB_RESULT`` line: the journals written, the ones already
   present, the lines, and the ledger's commit version.

Rerunning a run, or a new run over the same journals, writes nothing twice: the natural keys are the journal, its
content hash and the line.

**Foreign currencies (Stage 7c, ADR-0018).** The view gives each line its functional amount and the rate it was
converted at, and a converted journal's rounding line as one more line (``line_kind = 'fx_rounding'``, a zero
amount in the journal's currency). Debits and credits in the ledger are always functional.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from pyspark.sql import functions as F

from close_contracts.canonical_json import canonical_json
from close_contracts.fx import rate_text
from close_contracts.resolver import PathTableResolver
from close_contracts.table_schemas import REGISTRY
from close_contracts.tables import Tables
from close_engine import __version__ as ENGINE_VERSION
from close_engine.faults import pause_point
from close_engine.io.fence import Fence, FencedError
from close_engine.io.rows import insert_missing
from close_engine.io.tables import delta_table, read_table
from close_engine.jobs import RESULT_PREFIX
from close_engine.runctx import RunContext, add_contract_args, context_from_args
from close_engine.session import get_session

EXIT_FENCED: Final = 3
LINES: Final = REGISTRY[Tables.LEDGER_JOURNAL_LINES]
POSTINGS: Final = REGISTRY[Tables.LEDGER_JOURNAL_POSTINGS]
QUEUE_SQL: Final = (
    "SELECT journal_id, entity_id, number, revision, content_hash, template_id, period, accounting_date, currency, "
    "journal_description, source, preparer, approved_at, approvals, line_no, account, side, amount_minor, "
    "dimensions, line_description, functional_amount_minor, functional_currency, fx_rate, fx_rate_date, "
    "fx_rate_type, fx_rate_version, line_kind FROM posting_queue WHERE posting_run_id = %s AND entity_id = %s "
    "ORDER BY number, line_no"
)


def read_queue(run_id: UUID, entity_id: str) -> list[dict[str, Any]]:
    """The run's frozen lines, as ``close_ledger_reader`` (libpq ``PG*`` variables in the jobs service)."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(QUEUE_SQL, (run_id, entity_id))
        return list(cur.fetchall())


def ledger_rows(queue: list[dict[str, Any]], run_id: str, posted_at: datetime) -> list[dict[str, Any]]:
    out = []
    for q in queue:
        amount = int(q["amount_minor"])
        functional = int(q["functional_amount_minor"]) if q.get("functional_amount_minor") is not None else amount
        out.append(
            {
                "journal_id": str(q["journal_id"]),
                "entity_id": q["entity_id"],
                "number": int(q["number"]),
                "revision": int(q["revision"]),
                "content_hash": q["content_hash"],
                "line_no": int(q["line_no"]),
                "template_id": q["template_id"],
                "source": q["source"],
                "period": q["period"],
                "accounting_date": q["accounting_date"],
                "account": q["account"],
                "side": q["side"],
                "amount_minor": amount,
                "currency": q["currency"],
                "functional_amount_minor": functional,
                "functional_currency": q.get("functional_currency") or q["currency"],
                "debit_minor": functional if q["side"] == "DR" else 0,
                "credit_minor": functional if q["side"] == "CR" else 0,
                "dimensions": dict(q["dimensions"] or {}),
                "line_description": q["line_description"],
                "journal_description": q["journal_description"],
                "preparer": q["preparer"],
                "approvals": q["approvals"] or "",
                "approved_at": q["approved_at"],
                "run_id": run_id,
                "posted_at": posted_at,
                "fx_rate": q.get("fx_rate"),
                "fx_rate_date": q.get("fx_rate_date"),
                "fx_rate_type": q.get("fx_rate_type"),
                "fx_rate_version": q.get("fx_rate_version"),
                "line_kind": q.get("line_kind") or "entry",
            }
        )
    return out


def posting_rows(lines: list[dict[str, Any]], run_id: str, posted_at: datetime) -> list[dict[str, Any]]:
    """One row per journal for the ERP feed, its payload the journal's canonical JSON."""
    by_journal: dict[str, list[dict[str, Any]]] = {}
    for ln in lines:
        by_journal.setdefault(ln["journal_id"], []).append(ln)
    out = []
    for journal_id, its in by_journal.items():
        head = its[0]
        payload: Any = {
            "journal_id": journal_id,
            "entity_id": head["entity_id"],
            "number": head["number"],
            "period": head["period"],
            "accounting_date": head["accounting_date"].isoformat(),
            "currency": head["currency"],
            "functional_currency": head["functional_currency"],
            "rate": None
            if head.get("fx_rate") is None
            else {
                "rate": rate_text(head["fx_rate"]),
                "rate_date": head["fx_rate_date"].isoformat(),
                "rate_type": head["fx_rate_type"],
                "version": head["fx_rate_version"],
            },
            "description": head["journal_description"],
            "content_hash": head["content_hash"],
            "lines": [
                {
                    "line_no": ln["line_no"],
                    "kind": ln["line_kind"],
                    "account": ln["account"],
                    "side": ln["side"],
                    "amount_minor": ln["amount_minor"],
                    "functional_amount_minor": ln["functional_amount_minor"],
                    "dimensions": ln["dimensions"],
                    "description": ln["line_description"],
                }
                for ln in its
            ],
        }
        out.append(
            {
                "journal_id": journal_id,
                "entity_id": head["entity_id"],
                "number": head["number"],
                "period": head["period"],
                "content_hash": head["content_hash"],
                "payload": canonical_json(payload).decode(),
                "run_id": run_id,
                "posted_at": posted_at,
            }
        )
    return out


def _parse(argv: Sequence[str] | None) -> tuple[RunContext, str, Path | None]:
    parser = argparse.ArgumentParser(prog="close-job post_journals", description=__doc__)
    add_contract_args(parser)
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--result-file", type=Path, default=None, help="also write the JSON result here")
    args = parser.parse_args(argv)
    return context_from_args(parser, args), args.entity_id, args.result_file


def _emit(result: dict[str, object], result_file: Path | None) -> None:
    line = json.dumps(result, sort_keys=True, default=str)
    sys.stdout.write(f"{RESULT_PREFIX}{line}\n")
    sys.stdout.flush()
    if result_file is not None:
        result_file.write_text(line + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    ctx, entity_id, result_file = _parse(argv)
    spark = get_session("close-post-journals")
    resolver = PathTableResolver(lake_root=ctx.root_uri)
    fence = Fence(spark, resolver, ctx, job="post_journals")
    base: dict[str, object] = {
        "job": "post_journals",
        "run_id": ctx.run_id,
        "attempt": ctx.attempt,
        "fencing_token": ctx.fencing_token,
        "entity_id": entity_id,
        "engine_version": ENGINE_VERSION,
    }
    fence.claim()
    pause_point("after_claim")
    queue = read_queue(UUID(hex=ctx.run_id), entity_id)
    posted_at = ctx.requested_at  # the run's clock (Postgres time at the request): jobs never read the wall clock
    lines = ledger_rows(queue, ctx.run_id, posted_at)
    journals = sorted({ln["journal_id"] for ln in lines})
    in_entity = F.col("entity_id") == F.lit(entity_id)
    present = {
        str(r["journal_id"])
        for r in read_table(spark, resolver, Tables.LEDGER_JOURNAL_LINES)
        .where(in_entity & F.col("journal_id").isin(journals or [""]))
        .select("journal_id")
        .distinct()
        .collect()
    }
    try:
        fence.check("before_commit")
    except FencedError as err:
        _emit({**base, "fenced": True, "fenced_stage": err.stage, "highest_token": err.highest}, result_file)
        return EXIT_FENCED
    pause_point("before_commit")
    metadata = ctx.commit_metadata()
    if lines:
        insert_missing(
            spark, resolver, LINES, lines, within=F.col("t.entity_id") == F.lit(entity_id), metadata=metadata
        )
    commit_version = int(delta_table(spark, resolver, Tables.LEDGER_JOURNAL_LINES).history(1).collect()[0]["version"])
    pause_point("after_lines_commit")
    try:
        fence.check("before_feed")  # every commit is preceded by a check (ADR-0007)
    except FencedError as err:
        _emit({**base, "fenced": True, "fenced_stage": err.stage, "highest_token": err.highest}, result_file)
        return EXIT_FENCED
    if lines:
        within = F.col("t.entity_id") == F.lit(entity_id)
        insert_missing(
            spark, resolver, POSTINGS, posting_rows(lines, ctx.run_id, posted_at), within=within, metadata=metadata
        )
    result: dict[str, object] = {
        **base,
        "fenced": False,
        "journals": len(journals),
        "written": [j for j in journals if j not in present],
        "already_present": [j for j in journals if j in present],
        "lines": len(lines),
        "commit_version": commit_version,
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
