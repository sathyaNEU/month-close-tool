"""The period close over HTTP (Stage 8b, ADR-0020).

    GET  /close/checklists?entity_id=&period=                    a period's checklist and its tasks
    POST /close/checklists                                       generate it from the active checklist (controller)
    GET  /close/tasks/{entity_id}/{period}/{task_id}             one task and its history
    POST /close/tasks/{entity_id}/{period}/{task_id}:prepare     its preparer prepares a manual task
    POST /close/tasks/{entity_id}/{period}/{task_id}:check       run an automatic task's check
    POST /close/tasks/{entity_id}/{period}/{task_id}:review      its reviewer signs it off
    POST /close/tasks/{entity_id}/{period}/{task_id}:send-back   its reviewer sends it back (a reason)
    POST /close/tasks/{entity_id}/{period}/{task_id}:reopen      the controller reopens it (a reason)
    POST /close/tasks/{entity_id}/{period}/{task_id}:reassign    the controller reassigns it
    GET  /flux?entity_id=&period=                                each account against the prior period
    POST /flux/explanations                                      explain a line (bound to the server's amounts)
    POST /flux/reviews                                           sign off the period's flux

Reads need ``journal.read`` in the entity. Generating, reopening and reassigning need ``period.manage``; preparing,
checking and explaining ``close.prepare``; reviewing and sending back ``close.review``. Acting on a task assigned to
someone else is a 409. Generating, checking, explaining and reviewing read the calendar or the GL before their
transaction, after the idempotent replay check, like 7c's rate lookup.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection
from starlette.convertors import Convertor, register_url_convertor

from close_app import idempotency
from close_app.authz import Action, access
from close_app.close import checklists, checks, flux
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.guard import Caller, require
from close_app.identity import Principal
from close_app.logs import request_id_var
from close_app.routers import periods as _periods  # noqa: F401 (registers the period convertor first)
from close_app.routers.journals import readable_entities
from close_contracts.config.envelope import PRINCIPAL
from close_contracts.config.journal_workflow import PERIOD
from close_contracts.config.journaling import ACCOUNT_CODE, ENTITY


class TaskConvertor(Convertor[str]):
    """A task id, so ``.../carrier_accrual:prepare`` splits into the task and the verb."""

    regex = r"[a-z][a-z0-9_]{0,40}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("task", TaskConvertor())
router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header()]
Entity = Annotated[str, Query(pattern=ENTITY)]
Period = Annotated[str, Query(pattern=PERIOD)]
TASK_PATH = "/close/tasks/{entity_id}/{period:period}/{task_id:task}"


class PeriodBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=ENTITY)
    period: str = Field(pattern=PERIOD)


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, min_length=1, max_length=4000)


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class ReassignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preparer: str | None = Field(default=None, pattern=PRINCIPAL)
    reviewer: str | None = Field(default=None, pattern=PRINCIPAL)


class ExplainBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=ENTITY)
    period: str = Field(pattern=PERIOD)
    account: str = Field(pattern=ACCOUNT_CODE)
    explanation: str = Field(min_length=1, max_length=4000)


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _task_target(entity_id: str, period: str, task_id: str) -> str:
    return checklists.target(entity_id, period, task_id)


# ---------------------------------------------------------------------------------------------- checklists


@router.get("/close/checklists", openapi_extra=access(Action.JOURNAL_READ), name="get_close_checklist")
def get_close_checklist(request: Request, p: Caller, entity_id: Entity, period: Period) -> dict[str, Any]:
    readable_entities(p, Action.JOURNAL_READ, entity_id)
    with _ctx(request).engine.connect() as conn:
        return checklists.checklist_json(conn, entity_id, period)


@router.post(
    "/close/checklists", status_code=201, openapi_extra=access(Action.PERIOD_MANAGE), name="generate_close_checklist"
)
def generate_close_checklist(
    request: Request,
    p: Caller,
    body: PeriodBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.PERIOD_MANAGE, [body.entity_id], target=checklists.target(body.entity_id, body.period))
    ctx = _ctx(request)
    payload = body.model_dump(mode="json")
    if (stored := idempotency.replay(ctx.engine, request, p.user_id, payload)) is not None:
        return stored
    made = checklists.plan(ctx.engine, ctx.calendars, body.entity_id, body.period)  # the calendar, outside

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        out = checklists.generate(tx, made, body.entity_id, body.period, p=p, request_id=request_id_var.get())
        return 201, out, checklists.target(body.entity_id, body.period)

    return idempotency.run_once(ctx.engine, request, p.user_id, payload, effect)


# ---------------------------------------------------------------------------------------------- tasks


@router.get(TASK_PATH, openapi_extra=access(Action.JOURNAL_READ), name="get_close_task")
def get_close_task(request: Request, p: Caller, entity_id: str, period: str, task_id: str) -> dict[str, Any]:
    require(p, Action.JOURNAL_READ, [entity_id], target=_task_target(entity_id, period, task_id))
    with _ctx(request).engine.connect() as conn:
        row = checklists.task(conn, entity_id, period, task_id)
        return checklists.task_json(row) | {"history": checklists.history(conn, entity_id, period, task_id)}


def _move(
    request: Request,
    p: Principal,
    *,
    entity_id: str,
    period: str,
    task_id: str,
    action: Action,
    payload: dict[str, Any],
    step: Any,
) -> Any:
    require(p, action, [entity_id], target=_task_target(entity_id, period, task_id))
    ctx = _ctx(request)

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = step(tx)
        return 200, checklists.task_json(row), _task_target(entity_id, period, task_id)

    return idempotency.run_once(ctx.engine, request, p.user_id, payload, effect)


@router.post(f"{TASK_PATH}:prepare", openapi_extra=access(Action.CLOSE_PREPARE), name="prepare_close_task")
def prepare_close_task(
    request: Request,
    p: Caller,
    *,
    entity_id: str,
    period: str,
    task_id: str,
    body: NoteBody | None = None,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    note = (body or NoteBody()).note
    return _move(
        request,
        p,
        entity_id=entity_id,
        period=period,
        task_id=task_id,
        action=Action.CLOSE_PREPARE,
        payload={"note": note},
        step=lambda tx: checklists.prepare(
            tx, entity_id, period, task_id, p=p, note=note, request_id=request_id_var.get()
        ),
    )


@router.post(f"{TASK_PATH}:check", openapi_extra=access(Action.CLOSE_PREPARE), name="check_close_task")
def check_close_task(
    request: Request,
    p: Caller,
    *,
    entity_id: str,
    period: str,
    task_id: str,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.CLOSE_PREPARE, [entity_id], target=_task_target(entity_id, period, task_id))
    ctx = _ctx(request)
    payload: dict[str, Any] = {}
    if (stored := idempotency.replay(ctx.engine, request, p.user_id, payload)) is not None:
        return stored
    with ctx.engine.connect() as conn:
        row = checklists.checkable(conn, entity_id, period, task_id)
    passed, evidence, started = checks.run(ctx, p, str(row["check_name"]), entity_id, period)  # outside

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        new = checklists.record_check(
            tx,
            entity_id,
            period,
            task_id,
            p=p,
            passed=passed,
            evidence=evidence,
            checked_at=started,
            request_id=request_id_var.get(),
        )
        return 200, checklists.task_json(new) | {"passed": passed}, _task_target(entity_id, period, task_id)

    return idempotency.run_once(ctx.engine, request, p.user_id, payload, effect)


@router.post(f"{TASK_PATH}:review", openapi_extra=access(Action.CLOSE_REVIEW), name="review_close_task")
def review_close_task(
    request: Request,
    p: Caller,
    *,
    entity_id: str,
    period: str,
    task_id: str,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    return _move(
        request,
        p,
        entity_id=entity_id,
        period=period,
        task_id=task_id,
        action=Action.CLOSE_REVIEW,
        payload={},
        step=lambda tx: checklists.review(tx, entity_id, period, task_id, p=p, request_id=request_id_var.get()),
    )


@router.post(f"{TASK_PATH}:send-back", openapi_extra=access(Action.CLOSE_REVIEW), name="send_back_close_task")
def send_back_close_task(
    request: Request,
    p: Caller,
    *,
    entity_id: str,
    period: str,
    task_id: str,
    body: ReasonBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    return _move(
        request,
        p,
        entity_id=entity_id,
        period=period,
        task_id=task_id,
        action=Action.CLOSE_REVIEW,
        payload=body.model_dump(mode="json"),
        step=lambda tx: checklists.send_back(
            tx, entity_id, period, task_id, p=p, reason=body.reason, request_id=request_id_var.get()
        ),
    )


@router.post(f"{TASK_PATH}:reopen", openapi_extra=access(Action.PERIOD_MANAGE), name="reopen_close_task")
def reopen_close_task(
    request: Request,
    p: Caller,
    *,
    entity_id: str,
    period: str,
    task_id: str,
    body: ReasonBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    return _move(
        request,
        p,
        entity_id=entity_id,
        period=period,
        task_id=task_id,
        action=Action.PERIOD_MANAGE,
        payload=body.model_dump(mode="json"),
        step=lambda tx: checklists.reopen(
            tx, entity_id, period, task_id, p=p, reason=body.reason, request_id=request_id_var.get()
        ),
    )


@router.post(f"{TASK_PATH}:reassign", openapi_extra=access(Action.PERIOD_MANAGE), name="reassign_close_task")
def reassign_close_task(
    request: Request,
    p: Caller,
    *,
    entity_id: str,
    period: str,
    task_id: str,
    body: ReassignBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    if body.preparer is None and body.reviewer is None:
        raise Problem(422, "Name a new preparer or reviewer", kind="validation-failed")
    return _move(
        request,
        p,
        entity_id=entity_id,
        period=period,
        task_id=task_id,
        action=Action.PERIOD_MANAGE,
        payload=body.model_dump(mode="json"),
        step=lambda tx: checklists.reassign(
            tx,
            entity_id,
            period,
            task_id,
            p=p,
            preparer=body.preparer,
            reviewer=body.reviewer,
            request_id=request_id_var.get(),
        ),
    )


# ---------------------------------------------------------------------------------------------- flux


@router.get("/flux", openapi_extra=access(Action.JOURNAL_READ), name="get_flux")
def get_flux(request: Request, p: Caller, entity_id: Entity, period: Period) -> dict[str, Any]:
    readable_entities(p, Action.JOURNAL_READ, entity_id)
    return flux.compute(_ctx(request), p, entity_id, period)


@router.post(
    "/flux/explanations", status_code=201, openapi_extra=access(Action.CLOSE_PREPARE), name="explain_flux_line"
)
def explain_flux_line(
    request: Request,
    p: Caller,
    body: ExplainBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.CLOSE_PREPARE, [body.entity_id], target=f"flux:{body.entity_id}:{body.period}:{body.account}")
    ctx = _ctx(request)
    payload = body.model_dump(mode="json")
    if (stored := idempotency.replay(ctx.engine, request, p.user_id, payload)) is not None:
        return stored
    current = flux.compute(ctx, p, body.entity_id, body.period)  # the server's amounts, outside the transaction

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        out = flux.explain(tx, current, body.account, body.explanation, p=p, request_id=request_id_var.get())
        return 201, out, f"flux:{body.entity_id}:{body.period}:{body.account}"

    return idempotency.run_once(ctx.engine, request, p.user_id, payload, effect)


@router.post("/flux/reviews", status_code=201, openapi_extra=access(Action.CLOSE_REVIEW), name="review_flux")
def review_flux(
    request: Request,
    p: Caller,
    body: PeriodBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.CLOSE_REVIEW, [body.entity_id], target=f"flux:{body.entity_id}:{body.period}")
    ctx = _ctx(request)
    payload = body.model_dump(mode="json")
    if (stored := idempotency.replay(ctx.engine, request, p.user_id, payload)) is not None:
        return stored
    current = flux.compute(ctx, p, body.entity_id, body.period)

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        out = flux.review(tx, current, p=p, request_id=request_id_var.get())
        return 201, out, f"flux:{body.entity_id}:{body.period}"

    return idempotency.run_once(ctx.engine, request, p.user_id, payload, effect)
