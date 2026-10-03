"""Delegations and notifications over HTTP (Stage 7a).

GET  /delegations                          the caller's delegations, given or received
POST /delegations                          hand one's approval authority to a colleague for a date window
POST /delegations/{id}:revoke              end one early (the delegator, or a platform admin)
GET  /notifications                        the caller's reminders, escalations and skipped recurring journals
POST /notifications/{id}:read              mark one read
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from close_app import audit, idempotency
from close_app.authz import GLOBAL, Action, access, holds
from close_app.context import AppContext
from close_app.guard import Caller, not_found
from close_app.journals import delegations
from close_app.logs import request_id_var
from close_contracts.config.journaling import ENTITY

router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header()]


class DelegationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegate: str = Field(min_length=1, max_length=255)
    entity_ids: list[Annotated[str, Field(pattern=ENTITY)]] = Field(min_length=1, max_length=50)
    starts_on: date
    ends_on: date


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


@router.get("/delegations", openapi_extra=access("authenticated"), name="list_delegations")
def list_delegations(request: Request, p: Caller) -> dict[str, Any]:
    with _ctx(request).engine.connect() as conn:
        return {"items": delegations.mine(conn, p.user_id)}


@router.post("/delegations", status_code=201, openapi_extra=access(Action.JOURNAL_DELEGATE), name="create_delegation")
def create_delegation(
    request: Request,
    p: Caller,
    body: DelegationBody,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = delegations.create(
            tx,
            p,
            delegate=body.delegate,
            entity_ids=body.entity_ids,
            starts_on=body.starts_on,
            ends_on=body.ends_on,
            request_id=request_id_var.get(),
        )
        return 201, delegations.as_json(row), f"delegation:{row['delegation_id']}"

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.post(
    "/delegations/{delegation_id:uuid}:revoke",
    openapi_extra=access(Action.JOURNAL_DELEGATE),
    name="revoke_delegation",
)
def revoke_delegation(
    request: Request,
    p: Caller,
    delegation_id: UUID,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = delegations.load_locked(tx, delegation_id)
        admin = holds(p.assignments, Action.ACCESS_GRANT, GLOBAL)
        if row is None or not (admin or p.user_id in (row["delegator"], row["delegate"])):
            raise not_found(f"delegation {delegation_id}")
        new = delegations.revoke(tx, row, p, request_id_var.get())
        return 200, delegations.as_json(new), f"delegation:{delegation_id}"

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, {}, effect)


def _note(row: Any) -> dict[str, Any]:
    return {
        k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v)
        for k, v in row.items()
        if k != "dedup_key"
    }


@router.get("/notifications", openapi_extra=access("authenticated"), name="list_notifications")
def list_notifications(request: Request, p: Caller) -> dict[str, Any]:
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT * FROM notifications WHERE user_id = :u "
                "ORDER BY read_at IS NOT NULL, created_at DESC, notification_id LIMIT 200"
            ),
            {"u": p.user_id},
        ).mappings()
        return {"items": [_note(r) for r in rows]}


@router.post(
    "/notifications/{notification_id:uuid}:read", openapi_extra=access("authenticated"), name="read_notification"
)
def read_notification(
    request: Request,
    p: Caller,
    notification_id: UUID,
    idempotency_key: IdempotencyKey = None,  # noqa: ARG001
) -> Any:
    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = (
            tx.execute(
                text(
                    "UPDATE notifications SET read_at = COALESCE(read_at, clock_timestamp()) "
                    "WHERE notification_id = :id AND user_id = :u RETURNING *"
                ),
                {"id": notification_id, "u": p.user_id},
            )
            .mappings()
            .first()
        )
        if row is None:  # someone else's notification is not visible
            raise not_found(f"notification {notification_id}")
        event = audit.Event(
            p.user_id,
            "notification.read",
            entity_ids=(row["entity_id"],),
            target=f"notification:{notification_id}",
            payload={"kind": row["kind"]},
        )
        audit.append(tx, event, request_id_var.get())
        return 200, _note(row), f"notification:{notification_id}"

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, {}, effect)
