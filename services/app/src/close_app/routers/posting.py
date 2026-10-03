"""Posting and the general ledger over HTTP (Stage 7b, ADR-0017).

    POST /posting/runs                          post the entity's approved journals (never the caller's own)
    GET  /posting/runs?entity_id=                posting runs in the caller's entities
    GET  /posting/runs/{run_id}                  one run, with its skipped journals and outcome
    POST /posting/status                         the ERP write-back: document numbers for posted journals
    GET  /posting/tie-out?entity_id=             posted journals in Postgres against the ledger in Delta, by period
    GET  /gl/trial-balance?entity_id=&period=    totals by account, from the whole ledger (Stage 8a: rollups too)
    GET  /gl/lines?entity_id=&period=&account=   posted lines, paged, each with its journal
    GET  /gl/postings?entity_id=                 the ERP feed: one row per posted journal, for machine clients

The ledger reads go through the read path (typed query specs; the query engine always applies the caller's
entities, narrowed here to those where the permission is held). Posting runs and the write-back are idempotent
and audited.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from close_app import idempotency
from close_app.authz import Action, access, holds
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.guard import Caller, deny, not_found, require
from close_app.identity import Principal
from close_app.journals import posting
from close_app.ledger import balances as gl
from close_app.logs import request_id_var
from close_app.readpath import run_query
from close_app.routers.journals import readable_entities
from close_contracts.config.journaling import ENTITY
from close_contracts.query import Aggregate, Eq, Filter, QuerySpec, Sort
from close_contracts.queryable import JOURNAL_LINES, JOURNAL_POSTINGS
from close_platform.query import Page

router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header()]
EntityFilter = Annotated[str | None, Query(pattern=ENTITY)]
PeriodFilter = Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
LINE_COLUMNS = (
    "journal_id", "number", "line_no", "period", "account", "side", "amount_minor", "currency", "debit_minor",
    "credit_minor", "line_description", "journal_description", "source", "entity_id", "functional_amount_minor",
    "functional_currency", "line_kind",
)  # fmt: skip
FEED_COLUMNS = ("journal_id", "entity_id", "number", "period", "content_hash", "payload", "run_id")


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=ENTITY)


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    journal_id: UUID
    document_id: str = Field(min_length=1, max_length=50)


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[Confirmation] = Field(min_length=1, max_length=500)


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _query(ctx: AppContext, spec: QuerySpec, p: Principal, entities: list[str], cursor: str | None = None) -> Page:
    return run_query(ctx, spec, p, entities, cursor)


# ---------------------------------------------------------------------------------------------- posting runs


@router.post("/posting/runs", status_code=202, openapi_extra=access(Action.JOURNAL_POST), name="start_posting")
def start_posting(
    request: Request,
    p: Caller,
    body: RunBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.JOURNAL_POST, [body.entity_id], target=f"posting:{body.entity_id}")
    ctx = _ctx(request)

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        try:
            run = posting.start(tx, ctx.coordinator, body.entity_id, p=p, request_id=request_id_var.get())
        except KeyError:
            raise Problem(503, "Posting isn't configured", "the post_journals job id is not set") from None
        return 202, posting.run_json(run), f"posting_run:{run['run_id']}"

    return idempotency.run_once(ctx.engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.get("/posting/runs", openapi_extra=access(Action.JOURNAL_READ), name="list_posting_runs")
def list_posting_runs(request: Request, p: Caller, entity_id: EntityFilter = None) -> dict[str, Any]:
    entities = readable_entities(p, Action.JOURNAL_READ, entity_id)
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM posting_runs WHERE entity_id = ANY (:e) ORDER BY requested_at DESC LIMIT 200"),
            {"e": entities},
        ).mappings()
        return {"items": [posting.run_json(r) for r in rows]}


@router.get("/posting/runs/{run_id:uuid}", openapi_extra=access(Action.JOURNAL_READ), name="get_posting_run")
def get_posting_run(request: Request, p: Caller, run_id: UUID) -> dict[str, Any]:
    with _ctx(request).engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM posting_runs WHERE run_id = :id"), {"id": run_id}).mappings().first()
    if row is None:
        raise not_found(f"posting run {run_id}")
    require(p, Action.JOURNAL_READ, [row["entity_id"]], target=f"posting_run:{run_id}")
    return posting.run_json(row)


@router.post("/posting/status", openapi_extra=access(Action.JOURNAL_CONFIRM), name="confirm_postings")
def confirm_postings(
    request: Request,
    p: Caller,
    body: StatusBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    allowed = {e for e in p.entities if holds(p.assignments, Action.JOURNAL_CONFIRM, e)}
    if not allowed:
        raise deny(p, Action.JOURNAL_CONFIRM, "posting:status")
    entries = [{"journal_id": str(e.journal_id), "document_id": e.document_id} for e in body.entries]

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        confirmed = posting.confirm(tx, entries, p=p, entity_ids=allowed, request_id=request_id_var.get())
        return 200, {"confirmed": confirmed}, None

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.get("/posting/tie-out", openapi_extra=access(Action.JOURNAL_EXPORT), name="posting_tie_out")
def posting_tie_out(request: Request, p: Caller, entity_id: EntityFilter = None) -> dict[str, Any]:
    """Posted journals in Postgres against the lines in the Delta ledger, per entity and period, by line count
    (rounding lines included), and functional debits and credits (AS 1105 completeness; one currency per entity,
    so the sums never mix currencies). A difference is reported, never smoothed over."""
    entities = readable_entities(p, Action.JOURNAL_EXPORT, entity_id)
    ctx = _ctx(request)
    with ctx.engine.connect() as conn:
        pg = {
            (str(r.entity_id), str(r.period)): (int(r.lines), int(r.debits), int(r.credits))
            for r in conn.execute(
                text(
                    "SELECT entity_id, period, sum(ledger_line_count) AS lines, sum(functional_debit_minor) AS debits, "
                    "sum(functional_credit_minor) AS credits FROM journals "
                    "WHERE entity_id = ANY (:e) AND status IN ('posted', 'erp_confirmed') GROUP BY entity_id, period"
                ),
                {"e": entities},
            )
        }
    spec = QuerySpec(
        JOURNAL_LINES.key,
        group_by=("entity_id", "period"),
        aggregates=(
            Aggregate("count", "lines"),
            Aggregate("sum", "debits", "debit_minor"),
            Aggregate("sum", "credits", "credit_minor"),
        ),
        limit=1000,
    )
    delta = {
        (str(r["entity_id"]), str(r["period"])): (int(r["lines"] or 0), int(r["debits"] or 0), int(r["credits"] or 0))
        for r in _query(ctx, spec, p, entities).rows
    }
    items = []
    for key in sorted(set(pg) | set(delta)):
        mine, ledger = pg.get(key, (0, 0, 0)), delta.get(key, (0, 0, 0))
        items.append(
            {
                "entity_id": key[0],
                "period": key[1],
                "postgres": {"lines": mine[0], "debits": mine[1], "credits": mine[2]},
                "ledger": {"lines": ledger[0], "debits": ledger[1], "credits": ledger[2]},
                "ties": mine == ledger,
            }
        )
    return {"items": items, "ties": all(i["ties"] for i in items)}


# ---------------------------------------------------------------------------------------------- the ledger


@router.get("/gl/trial-balance", openapi_extra=access(Action.JOURNAL_READ), name="trial_balance")
def trial_balance(
    request: Request, p: Caller, entity_id: Annotated[str, Query(pattern=ENTITY)], period: PeriodFilter = None
) -> dict[str, Any]:
    readable_entities(p, Action.JOURNAL_READ, entity_id)
    filters: tuple[Filter, ...] = (Eq("period", period),) if period else ()
    # Stage 8a: the whole Databricks GL, the engine's rollups and the posted manual journals, in functional amounts
    found = gl.movements(_ctx(request), p, entity_id, filters)
    accounts = gl.trial_balance_rows((key[0], m) for key, m in found.items())
    debits = sum(int(r["debits"]) for r in accounts)
    credits = sum(int(r["credits"]) for r in accounts)
    return {
        "entity_id": entity_id,
        "period": period,
        "accounts": accounts,
        "debits": debits,
        "credits": credits,
        "balanced": debits == credits,
    }


@router.get("/gl/lines", openapi_extra=access(Action.JOURNAL_READ), name="ledger_lines")
def ledger_lines(
    request: Request,
    p: Caller,
    entity_id: Annotated[str, Query(pattern=ENTITY)],
    *,
    period: PeriodFilter = None,
    account: Annotated[str | None, Query(max_length=32)] = None,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
) -> dict[str, Any]:
    entities = readable_entities(p, Action.JOURNAL_READ, entity_id)
    filters: list[Filter] = []
    if period is not None:
        filters.append(Eq("period", period))
    if account is not None:
        filters.append(Eq("account", account))
    spec = QuerySpec(
        JOURNAL_LINES.key, LINE_COLUMNS, tuple(filters), (Sort("journal_id"), Sort("line_no")), limit=page_size
    )
    page = _query(_ctx(request), spec, p, entities, cursor)
    return {"lines": list(page.rows), "next_cursor": page.next_cursor, "version": page.version}


@router.get("/gl/postings", openapi_extra=access(Action.LEDGER_FEED), name="ledger_feed")
def ledger_feed(
    request: Request,
    p: Caller,
    entity_id: Annotated[str, Query(pattern=ENTITY)],
    *,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
) -> dict[str, Any]:
    """The ERP feed: posted journals, their canonical JSON included, for a machine client in the entity."""
    entities = readable_entities(p, Action.LEDGER_FEED, entity_id)
    spec = QuerySpec(JOURNAL_POSTINGS.key, FEED_COLUMNS, (), (Sort("journal_id"),), limit=page_size)
    page = _query(_ctx(request), spec, p, entities, cursor)
    return {"postings": list(page.rows), "next_cursor": page.next_cursor, "version": page.version}
