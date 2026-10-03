"""Accounting periods over HTTP (Stage 7a; D6: authoritative in Postgres).

    GET  /periods?entity_id=                          periods of the caller's entities
    POST /periods                                     open a period ({entity_id, period})
    GET  /periods/{entity_id}/{period}                one period (ETag)
    POST /periods/{entity_id}/{period}:soft-close|close|lock|reopen   move it (If-Match; reopen needs a reason)

Reading needs ``journal.read`` in the entity; every change needs ``period.manage`` there (the controller).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, RowMapping, text
from starlette.convertors import Convertor, register_url_convertor

from close_app import idempotency
from close_app.authz import Action, access
from close_app.context import AppContext
from close_app.guard import Caller, not_found, require
from close_app.identity import Principal
from close_app.journals import periods
from close_app.journals.setup import active
from close_app.logs import request_id_var
from close_app.routers.journals import check_if_match, readable_entities
from close_contracts.config.journal_workflow import PERIOD
from close_contracts.config.journaling import ENTITY


class PeriodConvertor(Convertor[str]):
    """``YYYY-MM``, so ``/periods/US01/2026-07:close`` splits into the period and the verb."""

    regex = r"[0-9]{4}-(?:0[1-9]|1[0-2])"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("period", PeriodConvertor())
router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header()]
IfMatch = Annotated[str | None, Header()]


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=ENTITY)
    period: str = Field(pattern=PERIOD)


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, min_length=1, max_length=2000)


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _period(
    conn: Connection, p: Principal, entity_id: str, period: str, action: Action, *, lock: bool = False
) -> RowMapping:
    require(p, action, [entity_id], target=f"period:{entity_id}:{period}")
    row = periods.load(conn, entity_id, period, lock=lock)
    if row is None:
        raise not_found(f"period {entity_id} {period}")
    return row


@router.get("/periods", openapi_extra=access(Action.JOURNAL_READ), name="list_periods")
def list_periods(
    request: Request, p: Caller, entity_id: Annotated[str | None, Query(pattern=ENTITY)] = None
) -> dict[str, Any]:
    entities = readable_entities(p, Action.JOURNAL_READ, entity_id)
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM accounting_periods WHERE entity_id = ANY (:e) ORDER BY entity_id, period DESC"),
            {"e": entities},
        ).mappings()
        return {"items": [periods.as_json(r) for r in rows]}


@router.post("/periods", status_code=201, openapi_extra=access(Action.PERIOD_MANAGE), name="open_period")
def open_period(
    request: Request,
    p: Caller,
    body: OpenBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.PERIOD_MANAGE, [body.entity_id], target=f"period:{body.entity_id}:{body.period}")

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = periods.open_new(
            tx, active(tx), body.entity_id, body.period, actor=p.user_id, request_id=request_id_var.get()
        )
        return 201, periods.as_json(row), periods.target(row)

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.get("/periods/{entity_id}/{period:period}", openapi_extra=access(Action.JOURNAL_READ), name="get_period")
def get_period(request: Request, p: Caller, entity_id: str, period: str) -> JSONResponse:
    with _ctx(request).engine.connect() as conn:
        row = _period(conn, p, entity_id, period, Action.JOURNAL_READ)
    return JSONResponse(periods.as_json(row), headers={"ETag": periods.etag(row)})


def _register_move(verb: str) -> None:
    @router.post(
        f"/periods/{{entity_id}}/{{period:period}}:{verb}",
        openapi_extra=access(Action.PERIOD_MANAGE),
        name=f"{verb.replace('-', '_')}_period",
    )
    def move(
        request: Request,
        p: Caller,
        entity_id: str,
        period: str,
        *,
        body: MoveBody | None = None,
        idempotency_key: IdempotencyKey = None,  # noqa: ARG001
        if_match: IfMatch = None,  # noqa: ARG001
    ) -> Any:
        payload = (body or MoveBody()).model_dump(mode="json")

        def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
            row = _period(tx, p, entity_id, period, Action.PERIOD_MANAGE, lock=True)
            check_if_match(request, periods.etag(row))
            new = periods.move(
                tx, row, verb, actor=p.user_id, reason=payload["reason"], request_id=request_id_var.get()
            )
            return 200, periods.as_json(new), periods.target(new)

        return idempotency.run_once(_ctx(request).engine, request, p.user_id, payload, effect)


for _verb in ("soft-close", "close", "lock", "reopen"):
    _register_move(_verb)
