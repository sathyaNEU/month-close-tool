"""Journals over HTTP (Stage 7a, ADR-0016).

    GET  /journals                                       journals in the caller's entities (by entity, status, period)
    POST /journals                                       a new draft
    GET  /journals/{id}                                  one journal with its lines and current approvals (ETag)
    PUT  /journals/{id}                                  edit a draft (If-Match)
    POST /journals/{id}:submit|approve|reject|recall|cancel   move it (If-Match)
    POST /journals/{id}:reverse                          a reversing draft of an approved journal
    GET  /journals/{id}/history                          every move, with actors and reasons
    GET  /journals/{id}/attachments                      supporting documents (metadata)
    POST /journals/{id}/attachments                      add one (base64 JSON; drafts only)
    GET  /journals/{id}/attachments/{a}                  download one
    POST /journals/{id}/attachments/{a}:remove           mark one removed (drafts only)
    POST /journal-imports?entity_id=                     a CSV file or an XLSX workbook of journals, all or nothing
    GET  /journal-imports/{import_id}                    an import's record
    GET  /journal-batches, POST /journal-batches         batches
    GET  /journal-batches/{id}                           one batch and its journals (ETag)
    POST /journal-batches/{id}/journals                  add a draft to a draft batch (If-Match)
    POST /journal-batches/{id}/journals/{j}:remove       take it out again (If-Match)
    POST /journal-batches/{id}:submit|approve|reject|recall|cancel   (If-Match)
    GET  /journal-population, /journal-population/summary, /journal-exceptions   evidence for auditors

- **Entity scope.** A journal in none of the caller's entities is a 404; a visible one without the permission is a
  403. Approving and rejecting need the level's authority, held directly or by delegation (checked by the workflow).
- **POSTs** go through ``Idempotency-Key``. Moves need ``If-Match``, checked after the idempotency lookup.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, RowMapping, text

from close_app import idempotency
from close_app.authz import Action, access, holds
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.guard import Caller, deny, not_found, require
from close_app.identity import Principal
from close_app.journals import attachments, batches, evidence, imports, store, workflow, xlsx
from close_app.journals.setup import active
from close_app.logs import request_id_var
from close_contracts.config.journaling import ENTITY
from close_contracts.fx import Rate, RateKey
from close_contracts.journals import JournalDraft
from close_platform.query import QueryBusyError, QueryTimeoutError

router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header()]
IfMatch = Annotated[str | None, Header()]
EntityFilter = Annotated[str | None, Query(pattern=ENTITY)]


class StepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, min_length=1, max_length=2000)  # reject
    accounting_date: date | None = None  # reverse: the reversal's date (default: the next period's first day)


class AttachmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=200)
    content_base64: str = Field(min_length=4, max_length=14_000_000)


class BatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=ENTITY)
    description: str = Field(min_length=1, max_length=500)


class MemberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    journal_id: UUID


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _rid() -> str | None:
    return request_id_var.get()


def check_if_match(request: Request, current: str) -> None:
    values = request.headers.getlist("if-match")
    if not values:
        raise Problem(428, "If-Match is required", "send the ETag you last read", kind="precondition-required")
    if current not in {v.strip() for value in values for v in value.split(",")}:
        raise Problem(412, "It has changed", "reload it", kind="precondition-failed", current_etag=current)


def _journal(conn: Connection, p: Principal, journal_id: UUID, action: Action, *, lock: bool = False) -> RowMapping:
    row = store.load(conn, journal_id, lock=lock)
    if row is None:
        raise not_found(f"journal {journal_id}")
    require(p, action, [row["entity_id"]], target=store.target(row))
    return row


def lookup_rates(ctx: AppContext, keys: set[RateKey]) -> dict[RateKey, Rate]:
    """Rates through the narrow lookup, outside any transaction (Stage 7c, ADR-0018)."""
    if not keys:
        return {}
    if ctx.rates is None:
        raise Problem(503, "Exchange rates are unavailable", "the read path is not configured")
    try:
        return ctx.rates.rates(keys)
    except QueryBusyError:
        raise Problem(503, "The read path is busy", "retry shortly") from None
    except QueryTimeoutError:
        raise Problem(504, "The rate lookup timed out") from None


def needed_rates(ctx: AppContext, needs: dict[UUID, workflow.RateNeed], numbers: dict[UUID, int]) -> dict[UUID, Rate]:
    """Each journal's rate, by journal; a 422 naming every journal whose rate isn't published."""
    found = lookup_rates(ctx, {n.key for n in needs.values() if n.key is not None})
    missing = [(numbers[j], n.key) for j, n in needs.items() if n.key is not None and n.key not in found]
    if missing:
        issues = [
            {"code": "no_rate", "path": f"/journals/{number}", "message": f"no {key.label()} rate has been published"}
            for number, key in sorted(missing)
        ]
        raise Problem(
            422, "No exchange rate", f"{len(missing)} journal(s) have no rate", kind="validation-failed", issues=issues
        )
    return {j: found[n.key] for j, n in needs.items() if n.key is not None}


def readable_entities(p: Principal, action: Action, entity_id: str | None) -> list[str]:
    """The entities a list or export covers: the one asked for, or every visible one where ``action`` is held."""
    if entity_id is not None:
        require(p, action, [entity_id], target=f"entity:{entity_id}")
        return [entity_id]
    held = sorted(e for e in p.entities if holds(p.assignments, action, e))
    if not held:
        raise deny(p, action, "journals")
    return held


# ---------------------------------------------------------------------------------------------- journals


@router.get("/journals", openapi_extra=access(Action.JOURNAL_READ), name="list_journals")
def list_journals(
    request: Request,
    p: Caller,
    entity_id: EntityFilter = None,
    status: Annotated[str | None, Query(pattern=r"^[a-z_]{1,20}$")] = None,
    period: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
) -> dict[str, Any]:
    entities = readable_entities(p, Action.JOURNAL_READ, entity_id)
    sql = "SELECT * FROM journals WHERE entity_id = ANY (:e)"
    params: dict[str, Any] = {"e": entities}
    if status is not None:
        sql, params["s"] = sql + " AND status = :s", status
    if period is not None:
        sql, params["p"] = sql + " AND period = :p", period
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(text(sql + " ORDER BY entity_id, number DESC LIMIT 500"), params).mappings().all()
        return {"items": [store.as_json(conn, r, detail=False) for r in rows]}


@router.post("/journals", status_code=201, openapi_extra=access(Action.JOURNAL_PREPARE), name="create_journal")
def create_journal(
    request: Request,
    p: Caller,
    body: JournalDraft,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.JOURNAL_PREPARE, [body.entity_id], target=f"journal:{body.entity_id}:new")

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = workflow.create(tx, active(tx), body, created_by=p.user_id, actor=p.user_id, request_id=_rid())
        return 201, store.as_json(tx, row), store.target(row)

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.get("/journals/{journal_id:uuid}", openapi_extra=access(Action.JOURNAL_READ), name="get_journal")
def get_journal(request: Request, p: Caller, journal_id: UUID) -> JSONResponse:
    with _ctx(request).engine.connect() as conn:
        row = _journal(conn, p, journal_id, Action.JOURNAL_READ)
        return JSONResponse(store.as_json(conn, row), headers={"ETag": store.etag(row)})


@router.put("/journals/{journal_id:uuid}", openapi_extra=access(Action.JOURNAL_PREPARE), name="edit_journal")
def edit_journal(
    request: Request,
    p: Caller,
    journal_id: UUID,
    body: JournalDraft,
    if_match: IfMatch = None,  # noqa: ARG001 (read by check_if_match)
) -> JSONResponse:
    with _ctx(request).engine.begin() as tx:
        row = _journal(tx, p, journal_id, Action.JOURNAL_PREPARE, lock=True)
        check_if_match(request, store.etag(row))
        new = workflow.edit(tx, active(tx), row, body, p=p, request_id=_rid())
        out = store.as_json(tx, new)
    return JSONResponse(out, headers={"ETag": store.etag(new)})


def authorize(tx: Connection, verb: str, row: RowMapping, p: Principal, target: str) -> None:
    """The permission a move needs beyond seeing the object, checked before its state and its ETag, so a caller
    without it always gets a 403."""
    entity_id = row["entity_id"]
    if verb in ("approve", "reject") and not workflow.may_approve(tx, p, entity_id):
        raise deny(p, Action.JOURNAL_APPROVE_L1, target, [entity_id])
    if verb == "cancel" and not (
        holds(p.assignments, Action.JOURNAL_PREPARE, entity_id) or holds(p.assignments, Action.PERIOD_MANAGE, entity_id)
    ):
        raise deny(p, Action.JOURNAL_PREPARE, target, [entity_id])


def _step(
    tx: Connection, verb: str, row: RowMapping, p: Principal, payload: dict[str, Any], *, rate: Rate | None = None
) -> RowMapping:
    if verb == "submit":
        return workflow.submit(tx, active(tx), row, p=p, request_id=_rid(), rate=rate)
    if verb == "approve":
        return workflow.approve(tx, active(tx), row, p=p, request_id=_rid())
    if verb == "reject":
        if not payload["reason"]:
            raise Problem(422, "A rejection needs a reason", kind="validation-failed")
        return workflow.reject(tx, row, p=p, reason=payload["reason"], request_id=_rid())
    if verb == "recall":
        return workflow.recall(tx, row, p=p, request_id=_rid())
    return workflow.cancel(tx, row, p=p, request_id=_rid())


def _register_step(verb: str, action: Action) -> None:
    visibility = Action.JOURNAL_READ if verb in ("approve", "reject", "cancel") else action

    @router.post(f"/journals/{{journal_id:uuid}}:{verb}", openapi_extra=access(action), name=f"{verb}_journal")
    def step(
        request: Request,
        p: Caller,
        journal_id: UUID,
        *,
        body: StepBody | None = None,
        idempotency_key: IdempotencyKey = None,  # noqa: ARG001
        if_match: IfMatch = None,  # noqa: ARG001
    ) -> Any:
        payload = (body or StepBody()).model_dump(mode="json")
        ctx = _ctx(request)
        rate: Rate | None = None
        if verb == "submit":
            if (stored := idempotency.replay(ctx.engine, request, p.user_id, payload)) is not None:
                return stored
            try:
                rate = _submit_rate(request, p, journal_id)
            except Problem:  # the first of two duplicates may have committed since: answer as its replay
                if (stored := idempotency.replay(ctx.engine, request, p.user_id, payload)) is not None:
                    return stored
                raise

        def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
            row = _journal(tx, p, journal_id, visibility, lock=True)
            authorize(tx, verb, row, p, store.target(row))
            check_if_match(request, store.etag(row))
            new = _step(tx, verb, row, p, payload, rate=rate)
            return 200, store.as_json(tx, new), store.target(new)

        return idempotency.run_once(ctx.engine, request, p.user_id, payload, effect)


def _submit_rate(request: Request, p: Principal, journal_id: UUID) -> Rate | None:
    """What a submit checks before its transaction, in the order the transaction checks it (visibility,
    authority, ETag, state), then the rate a foreign-currency journal needs, looked up outside any transaction."""
    ctx = _ctx(request)
    with ctx.engine.connect() as conn:
        row = _journal(conn, p, journal_id, Action.JOURNAL_PREPARE)
        authorize(conn, "submit", row, p, store.target(row))
        check_if_match(request, store.etag(row))
        workflow.need_status(row, "draft", verb="submit")
        workflow.not_in_batch(row)
        workflow.preparer_only(p, row, "submit it")
        need = workflow.rate_need(conn, row)
    return needed_rates(ctx, {row["journal_id"]: need}, {row["journal_id"]: row["number"]}).get(row["journal_id"])


for _verb, _action in (
    ("submit", Action.JOURNAL_PREPARE),
    ("approve", Action.JOURNAL_APPROVE_L1),
    ("reject", Action.JOURNAL_APPROVE_L1),
    ("recall", Action.JOURNAL_PREPARE),
    ("cancel", Action.JOURNAL_PREPARE),
):
    _register_step(_verb, _action)


@router.post(
    "/journals/{journal_id:uuid}:reverse",
    status_code=201,
    openapi_extra=access(Action.JOURNAL_PREPARE),
    name="reverse_journal",
)
def reverse_journal(
    request: Request,
    p: Caller,
    journal_id: UUID,
    *,
    body: StepBody | None = None,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    payload = (body or StepBody()).model_dump(mode="json")

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = _journal(tx, p, journal_id, Action.JOURNAL_PREPARE, lock=True)
        day = date.fromisoformat(payload["accounting_date"]) if payload["accounting_date"] else None
        new = workflow.reverse(tx, active(tx), row, p=p, accounting_date=day, request_id=_rid())
        return 201, store.as_json(tx, new), store.target(new)

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, payload, effect)


@router.get("/journals/{journal_id:uuid}/history", openapi_extra=access(Action.JOURNAL_READ), name="journal_history")
def journal_history(request: Request, p: Caller, journal_id: UUID) -> dict[str, Any]:
    with _ctx(request).engine.connect() as conn:
        row = _journal(conn, p, journal_id, Action.JOURNAL_READ)
        rows = conn.execute(
            text("SELECT * FROM journal_history WHERE journal_id = :id ORDER BY history_id"), {"id": row["journal_id"]}
        ).mappings()
        items = [
            {
                k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v)
                for k, v in r.items()
            }
            for r in rows
        ]
    return {"items": items}


# ---------------------------------------------------------------------------------------------- attachments


@router.get(
    "/journals/{journal_id:uuid}/attachments", openapi_extra=access(Action.JOURNAL_READ), name="list_attachments"
)
def list_attachments(request: Request, p: Caller, journal_id: UUID) -> dict[str, Any]:
    with _ctx(request).engine.connect() as conn:
        return {"items": attachments.listing(conn, _journal(conn, p, journal_id, Action.JOURNAL_READ))}


@router.post(
    "/journals/{journal_id:uuid}/attachments",
    status_code=201,
    openapi_extra=access(Action.JOURNAL_PREPARE),
    name="add_attachment",
)
def add_attachment(
    request: Request,
    p: Caller,
    journal_id: UUID,
    body: AttachmentBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = _journal(tx, p, journal_id, Action.JOURNAL_PREPARE, lock=True)
        meta = attachments.add(
            tx,
            row,
            p=p,
            file_name=body.file_name,
            content_type=body.content_type,
            content_base64=body.content_base64,
            request_id=_rid(),
        )
        return 201, meta, store.target(row)

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.get(
    "/journals/{journal_id:uuid}/attachments/{attachment_id:uuid}",
    openapi_extra=access(Action.JOURNAL_READ),
    name="download_attachment",
)
def download_attachment(request: Request, p: Caller, journal_id: UUID, attachment_id: UUID) -> Response:
    with _ctx(request).engine.connect() as conn:
        row = _journal(conn, p, journal_id, Action.JOURNAL_READ)
        found = attachments.content(conn, row, attachment_id)
    if found is None:
        raise not_found(f"attachment {attachment_id}")
    fallback = "".join(c if (c.isascii() and c.isalnum()) or c in "._-" else "_" for c in found["file_name"])
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(found['file_name'], safe='')}"
    return Response(
        content=bytes(found["content"]),
        media_type=found["content_type"],
        headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@router.post(
    "/journals/{journal_id:uuid}/attachments/{attachment_id:uuid}:remove",
    openapi_extra=access(Action.JOURNAL_PREPARE),
    name="remove_attachment",
)
def remove_attachment(
    request: Request,
    p: Caller,
    journal_id: UUID,
    attachment_id: UUID,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = _journal(tx, p, journal_id, Action.JOURNAL_PREPARE, lock=True)
        return 200, attachments.remove(tx, row, attachment_id, p=p, request_id=_rid()), store.target(row)

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, {}, effect)


# ---------------------------------------------------------------------------------------------- imports


@router.post("/journal-imports", status_code=201, openapi_extra=access(Action.JOURNAL_PREPARE), name="import_journals")
def import_journals(
    request: Request,
    p: Caller,
    entity_id: Annotated[str, Query(pattern=ENTITY)],
    body: Annotated[bytes, Body(media_type="text/csv")],
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.JOURNAL_PREPARE, [entity_id], target=f"journal_import:{entity_id}")
    digest = hashlib.sha256(body).hexdigest()
    payload = {"entity_id": entity_id, "sha256": digest}
    if (stored := idempotency.replay(_ctx(request).engine, request, p.user_id, payload)) is not None:
        return stored  # a replay never parses the file again
    csv_body, refused = body, None
    if request.headers.get("content-type", "").split(";")[0].strip().lower() == xlsx.CONTENT_TYPE:
        try:  # before the transaction: no connection is held while an untrusted file is parsed
            csv_body = xlsx.to_csv(body)
        except Problem as exc:
            if exc.status != 422:
                raise
            refused = list(exc.extra.get("issues", []))

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        setup = active(tx)
        entity = setup.entity(entity_id)
        status, out = imports.run(tx, setup, entity, csv_body, p=p, request_id=_rid(), digest=digest, refused=refused)
        return status, out, f"journal_import:{out['import_id']}"

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, payload, effect)


@router.get("/journal-imports/{import_id:uuid}", openapi_extra=access(Action.JOURNAL_READ), name="get_import")
def get_import(request: Request, p: Caller, import_id: UUID) -> dict[str, Any]:
    with _ctx(request).engine.connect() as conn:
        found = imports.load(conn, import_id)
    if found is None:
        raise not_found(f"journal import {import_id}")
    require(p, Action.JOURNAL_READ, [found["entity_id"]], target=f"journal_import:{import_id}")
    return found


# ---------------------------------------------------------------------------------------------- batches


def _batch(conn: Connection, p: Principal, batch_id: UUID, action: Action, *, lock: bool = False) -> RowMapping:
    row = batches.load(conn, batch_id, lock=lock)
    if row is None:
        raise not_found(f"journal batch {batch_id}")
    require(p, action, [row["entity_id"]], target=batches.target(row))
    return row


@router.get("/journal-batches", openapi_extra=access(Action.JOURNAL_READ), name="list_batches")
def list_batches(request: Request, p: Caller, entity_id: EntityFilter = None) -> dict[str, Any]:
    entities = readable_entities(p, Action.JOURNAL_READ, entity_id)
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM journal_batches WHERE entity_id = ANY (:e) ORDER BY created_at DESC LIMIT 200"),
            {"e": entities},
        ).mappings()
        return {"items": [batches.as_json(conn, r) for r in rows]}


@router.post("/journal-batches", status_code=201, openapi_extra=access(Action.JOURNAL_PREPARE), name="create_batch")
def create_batch(
    request: Request,
    p: Caller,
    body: BatchBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    require(p, Action.JOURNAL_PREPARE, [body.entity_id], target=f"journal_batch:{body.entity_id}:new")

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = batches.create(tx, body.entity_id, body.description, p=p, request_id=_rid())
        return 201, batches.as_json(tx, row), batches.target(row)

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.get("/journal-batches/{batch_id:uuid}", openapi_extra=access(Action.JOURNAL_READ), name="get_batch")
def get_batch(request: Request, p: Caller, batch_id: UUID) -> JSONResponse:
    with _ctx(request).engine.connect() as conn:
        row = _batch(conn, p, batch_id, Action.JOURNAL_READ)
        return JSONResponse(batches.as_json(conn, row), headers={"ETag": batches.etag(row)})


@router.post(
    "/journal-batches/{batch_id:uuid}/journals", openapi_extra=access(Action.JOURNAL_PREPARE), name="add_to_batch"
)
def add_to_batch(
    request: Request,
    p: Caller,
    batch_id: UUID,
    *,
    body: MemberBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
    if_match: IfMatch = None,  # noqa: ARG001
) -> Any:
    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = _batch(tx, p, batch_id, Action.JOURNAL_PREPARE, lock=True)
        check_if_match(request, batches.etag(row))
        journal = _journal(tx, p, body.journal_id, Action.JOURNAL_PREPARE, lock=True)
        new = batches.add(tx, row, journal, p=p, request_id=_rid())
        return 200, batches.as_json(tx, new), batches.target(new)

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.post(
    "/journal-batches/{batch_id:uuid}/journals/{journal_id:uuid}:remove",
    openapi_extra=access(Action.JOURNAL_PREPARE),
    name="remove_from_batch",
)
def remove_from_batch(
    request: Request,
    p: Caller,
    batch_id: UUID,
    journal_id: UUID,
    *,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
    if_match: IfMatch = None,  # noqa: ARG001
) -> Any:
    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = _batch(tx, p, batch_id, Action.JOURNAL_PREPARE, lock=True)
        check_if_match(request, batches.etag(row))
        journal = _journal(tx, p, journal_id, Action.JOURNAL_PREPARE, lock=True)
        new = batches.remove(tx, row, journal, p=p, request_id=_rid())
        return 200, batches.as_json(tx, new), batches.target(new)

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, {}, effect)


def _batch_step(
    tx: Connection,
    verb: str,
    row: RowMapping,
    p: Principal,
    payload: dict[str, Any],
    *,
    rates: dict[UUID, Rate] | None = None,
) -> RowMapping:
    if verb == "submit":
        return batches.submit(tx, active(tx), row, p=p, request_id=_rid(), rates=rates)
    if verb == "approve":
        return batches.approve(tx, active(tx), row, p=p, request_id=_rid())
    if verb == "reject":
        if not payload["reason"]:
            raise Problem(422, "A rejection needs a reason", kind="validation-failed")
        return batches.reject(tx, row, p=p, reason=payload["reason"], request_id=_rid())
    if verb == "recall":
        return batches.recall(tx, row, p=p, request_id=_rid())
    return batches.cancel(tx, row, p=p, request_id=_rid())


def _register_batch_step(verb: str, action: Action) -> None:
    visibility = Action.JOURNAL_READ if verb in ("approve", "reject") else action

    @router.post(f"/journal-batches/{{batch_id:uuid}}:{verb}", openapi_extra=access(action), name=f"{verb}_batch")
    def step(
        request: Request,
        p: Caller,
        batch_id: UUID,
        *,
        body: StepBody | None = None,
        idempotency_key: IdempotencyKey = None,  # noqa: ARG001
        if_match: IfMatch = None,  # noqa: ARG001
    ) -> Any:
        payload = (body or StepBody()).model_dump(mode="json")
        ctx = _ctx(request)
        rates: dict[UUID, Rate] | None = None
        if verb == "submit":
            if (stored := idempotency.replay(ctx.engine, request, p.user_id, payload)) is not None:
                return stored
            try:
                rates = _batch_rates(request, p, batch_id)
            except Problem:  # as for a journal: a duplicate that lost the race replays
                if (stored := idempotency.replay(ctx.engine, request, p.user_id, payload)) is not None:
                    return stored
                raise

        def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
            row = _batch(tx, p, batch_id, visibility, lock=True)
            authorize(tx, verb, row, p, batches.target(row))
            check_if_match(request, batches.etag(row))
            new = _batch_step(tx, verb, row, p, payload, rates=rates)
            return 200, batches.as_json(tx, new), batches.target(new)

        return idempotency.run_once(ctx.engine, request, p.user_id, payload, effect)


def _batch_rates(request: Request, p: Principal, batch_id: UUID) -> dict[UUID, Rate]:
    """A batch submit's checks before its transaction, then every foreign-currency member's rate in one lookup."""
    ctx = _ctx(request)
    with ctx.engine.connect() as conn:
        row = _batch(conn, p, batch_id, Action.JOURNAL_PREPARE)
        authorize(conn, "submit", row, p, batches.target(row))
        check_if_match(request, batches.etag(row))
        batches.submittable(p, row)
        members = batches.members(conn, row)
        needs = {m["journal_id"]: workflow.rate_need(conn, m) for m in members}
        numbers = {m["journal_id"]: m["number"] for m in members}
    return needed_rates(ctx, needs, numbers)


for _verb, _action in (
    ("submit", Action.JOURNAL_PREPARE),
    ("approve", Action.JOURNAL_APPROVE_L1),
    ("reject", Action.JOURNAL_APPROVE_L1),
    ("recall", Action.JOURNAL_PREPARE),
    ("cancel", Action.JOURNAL_PREPARE),
):
    _register_batch_step(_verb, _action)


# ---------------------------------------------------------------------------------------------- evidence


@router.get("/journal-population", openapi_extra=access(Action.JOURNAL_EXPORT), name="journal_population")
def journal_population(request: Request, p: Caller, entity_id: EntityFilter = None) -> PlainTextResponse:
    entities = readable_entities(p, Action.JOURNAL_EXPORT, entity_id)
    with _ctx(request).engine.connect() as conn:
        body = evidence.population_csv(conn, entities)
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="journal-population.csv"', "Cache-Control": "no-store"},
    )


@router.get(
    "/journal-population/summary", openapi_extra=access(Action.JOURNAL_EXPORT), name="journal_population_summary"
)
def journal_population_summary(request: Request, p: Caller, entity_id: EntityFilter = None) -> dict[str, Any]:
    entities = readable_entities(p, Action.JOURNAL_EXPORT, entity_id)
    with _ctx(request).engine.connect() as conn:
        return {"items": evidence.summary(conn, entities)}


@router.get("/journal-exceptions", openapi_extra=access(Action.JOURNAL_EXPORT), name="journal_exceptions")
def journal_exceptions(request: Request, p: Caller, entity_id: EntityFilter = None) -> dict[str, Any]:
    entities = readable_entities(p, Action.JOURNAL_EXPORT, entity_id)
    with _ctx(request).engine.connect() as conn:
        return {"items": evidence.exceptions(conn, entities)}
