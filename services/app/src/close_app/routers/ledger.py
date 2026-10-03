"""The general ledger over HTTP (Stage 8a, ADR-0019).

    POST /journaling/runs                            journal an entity's data with the stand-in (the controller)
    GET  /journaling/runs?entity_id=                 journaling runs in the caller's entities
    GET  /journaling/runs/{run_id}                   one run and its outcome
    GET  /gl/balances?entity_id=&period=             each account's opening, activity and closing balance
    GET  /gl/roll-forward?entity_id=&from=&to=       the same per period, and whether each opens where the last closed
    POST /erp/trial-balances?entity_id=&period=      the ERP's trial balance as CSV (an integration client)
    GET  /erp/trial-balances?entity_id=&period=      every extract uploaded for the period, accepted or not
    GET  /gl/erp-tie-out?entity_id=&period=          the Databricks GL against the ERP's latest accepted extract

The GL reads go through the read path, so the caller's entities always apply. Runs and uploads are idempotent and
audited.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from close_app import idempotency
from close_app.authz import Action, access
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.guard import Caller, not_found, require
from close_app.journals.setup import active
from close_app.ledger import balances as gl
from close_app.ledger import erp, journaling
from close_app.logs import request_id_var
from close_app.routers.journals import readable_entities
from close_contracts.config.journaling import ENTITY

router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header()]
EntityFilter = Annotated[str | None, Query(pattern=ENTITY)]
Entity = Annotated[str, Query(pattern=ENTITY)]
Period = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=ENTITY)


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


# ---------------------------------------------------------------------------------------------- journaling runs


@router.post("/journaling/runs", status_code=202, openapi_extra=access(Action.JOURNALING_RUN), name="start_journaling")
def start_journaling(
    request: Request,
    p: Caller,
    body: RunBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.JOURNALING_RUN, [body.entity_id], target=f"journaling:{body.entity_id}")
    ctx = _ctx(request)

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        try:
            run = journaling.start(tx, ctx.coordinator, body.entity_id, p=p, request_id=request_id_var.get())
        except KeyError:
            raise Problem(503, "Journaling isn't configured", "the journal_ref job id is not set") from None
        return 202, journaling.run_json(run), f"journaling_run:{run['run_id']}"

    return idempotency.run_once(ctx.engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.get("/journaling/runs", openapi_extra=access(Action.JOURNAL_READ), name="list_journaling_runs")
def list_journaling_runs(request: Request, p: Caller, entity_id: EntityFilter = None) -> dict[str, Any]:
    entities = readable_entities(p, Action.JOURNAL_READ, entity_id)
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM journaling_runs WHERE entity_id = ANY (:e) ORDER BY requested_at DESC LIMIT 200"),
            {"e": entities},
        ).mappings()
        return {"items": [journaling.run_json(r) for r in rows]}


@router.get("/journaling/runs/{run_id:uuid}", openapi_extra=access(Action.JOURNAL_READ), name="get_journaling_run")
def get_journaling_run(request: Request, p: Caller, run_id: UUID) -> dict[str, Any]:
    with _ctx(request).engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM journaling_runs WHERE run_id = :id"), {"id": run_id}).mappings().first()
    if row is None:
        raise not_found(f"journaling run {run_id}")
    require(p, Action.JOURNAL_READ, [row["entity_id"]], target=f"journaling_run:{run_id}")
    return journaling.run_json(row)


# ---------------------------------------------------------------------------------------------- balances


@router.get("/gl/balances", openapi_extra=access(Action.JOURNAL_READ), name="gl_balances")
def gl_balances(request: Request, p: Caller, entity_id: Entity, period: Period) -> dict[str, Any]:
    readable_entities(p, Action.JOURNAL_READ, entity_id)
    return gl.balances(_ctx(request), p, entity_id, period)


@router.get("/gl/roll-forward", openapi_extra=access(Action.JOURNAL_READ), name="gl_roll_forward")
def gl_roll_forward(
    request: Request,
    p: Caller,
    entity_id: Entity,
    first: Annotated[str, Query(alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    last: Annotated[str, Query(alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> dict[str, Any]:
    readable_entities(p, Action.JOURNAL_READ, entity_id)
    return gl.roll_forward(_ctx(request), p, entity_id, first, last)


# ---------------------------------------------------------------------------------------------- the ERP's trial balance


@router.post(
    "/erp/trial-balances", status_code=201, openapi_extra=access(Action.ERP_EXTRACT), name="upload_erp_trial_balance"
)
def upload_erp_trial_balance(
    request: Request,
    p: Caller,
    *,
    entity_id: Entity,
    period: Period,
    body: Annotated[bytes, Body(media_type="text/csv")],
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.ERP_EXTRACT, [entity_id], target=f"erp_trial_balance:{entity_id}:{period}")
    digest = hashlib.sha256(body).hexdigest()
    payload = {"entity_id": entity_id, "period": period, "sha256": digest}
    ctx = _ctx(request)

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        currency = active(tx).entity(entity_id).functional_currency
        status, out = erp.upload(
            tx, entity_id, period, currency, body, digest=digest, p=p, request_id=request_id_var.get()
        )
        return status, out, f"erp_trial_balance:{out['extract_id']}"

    return idempotency.run_once(ctx.engine, request, p.user_id, payload, effect)


@router.get("/erp/trial-balances", openapi_extra=access(Action.JOURNAL_EXPORT), name="list_erp_trial_balances")
def list_erp_trial_balances(request: Request, p: Caller, entity_id: Entity, period: Period) -> dict[str, Any]:
    readable_entities(p, Action.JOURNAL_EXPORT, entity_id)
    with _ctx(request).engine.connect() as conn:
        return {"items": erp.extracts(conn, entity_id, period)}


@router.get("/gl/erp-tie-out", openapi_extra=access(Action.JOURNAL_EXPORT), name="gl_erp_tie_out")
def gl_erp_tie_out(request: Request, p: Caller, entity_id: Entity, period: Period) -> dict[str, Any]:
    readable_entities(p, Action.JOURNAL_EXPORT, entity_id)
    return erp.tie_out(_ctx(request), p, entity_id, period)
