"""Governance over HTTP: the audit log, the role catalog, role assignments and users (roadmap Backend API).

    GET  /audit-events                          events the caller may read, oldest first (``after`` = last seq)
    GET  /roles                                 the role catalog and each role's actions
    GET  /users                                 principals with their active assignments
    GET  /role-assignments                      active assignments (``user_id`` filter)
    POST /role-assignments                      grant (Idempotency-Key)
    POST /role-assignments/{id}:revoke          revoke (Idempotency-Key)

- **Audit events.** ``audit.read`` at ``GLOBAL`` shows every event, which a reviewer needs to check the hash chain
  end to end (you decided this at the Stage 4 sign-off). ``audit.read`` in an entity shows the events about that
  entity.
- **Access administration** is global. It needs ``access.read`` or ``access.grant`` at ``GLOBAL``, and nobody can
  grant a role to themselves.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from close_app import audit, idempotency
from close_app.authz import GLOBAL, POLICY, Action, Role, access, holds, holds_anywhere
from close_app.context import AppContext
from close_app.errors import Denied, Problem
from close_app.guard import Caller, deny, not_found, require
from close_app.logs import request_id_var

router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header()]
SCOPE = r"^(GLOBAL|[A-Z0-9]{2,16})$"

_EVENTS = text(
    "SELECT seq, recorded_at, actor, action, outcome, entity_ids, target, request_id, payload FROM audit_events "
    "WHERE seq > :after AND (:everything OR entity_ids && CAST(:entities AS text[])) "
    "AND (CAST(:action AS text) IS NULL OR action = :action) AND (CAST(:actor AS text) IS NULL OR actor = :actor) "
    "ORDER BY seq LIMIT :limit"
)
_ASSIGNMENTS = text(
    "SELECT assignment_id, user_id, role, scope, granted_by, granted_at FROM role_assignments "
    "WHERE revoked_at IS NULL AND (CAST(:u AS text) IS NULL OR user_id = :u) ORDER BY user_id, role, scope"
)


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=255, pattern=r"^[^\s,;]+$")
    role: Role
    scope: str = Field(pattern=SCOPE)


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


@router.get("/audit-events", openapi_extra=access(Action.AUDIT_READ))
def audit_events(
    request: Request,
    p: Caller,
    *,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor: Annotated[str | None, Query(max_length=255)] = None,
) -> dict[str, Any]:
    if not holds_anywhere(p.assignments, Action.AUDIT_READ):
        raise deny(p, Action.AUDIT_READ, "audit-events")
    scopes = sorted({s for _, s in p.assignments if s != GLOBAL and holds(p.assignments, Action.AUDIT_READ, s)})
    params = {
        "after": after,
        "limit": limit,
        "everything": holds(p.assignments, Action.AUDIT_READ, GLOBAL),
        "entities": scopes,
        "action": action,
        "actor": actor,
    }
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(_EVENTS, params).mappings().all()
    items = [
        {
            **dict(r),
            "recorded_at": r["recorded_at"].isoformat(),
            "entity_ids": list(r["entity_ids"]),
        }
        for r in rows
    ]
    return {"items": items, "next_after": items[-1]["seq"] if len(items) == limit else None}


@router.get("/roles", openapi_extra=access("authenticated"))
def roles(p: Caller) -> dict[str, Any]:  # noqa: ARG001 (signed-in principals only)
    return {"items": [{"role": r.value, "actions": sorted(a.value for a in POLICY[r])} for r in Role]}


def _require_global(p: Any, action: Action, target: str) -> None:
    require(p, action, None, target=target)


@router.get("/users", openapi_extra=access(Action.ACCESS_READ))
def users(request: Request, p: Caller) -> dict[str, Any]:
    _require_global(p, Action.ACCESS_READ, "users")
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(_ASSIGNMENTS, {"u": None}).mappings().all()
    by_user: dict[str, list[list[str]]] = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append([r["role"], r["scope"]])
    return {"items": [{"user_id": u, "roles": roles} for u, roles in sorted(by_user.items())]}


def _assignment_json(r: Any) -> dict[str, Any]:
    return {
        "assignment_id": str(r["assignment_id"]),
        "user_id": r["user_id"],
        "role": r["role"],
        "scope": r["scope"],
        "granted_by": r["granted_by"],
        "granted_at": r["granted_at"].isoformat(),
    }


@router.get("/role-assignments", openapi_extra=access(Action.ACCESS_READ))
def list_assignments(
    request: Request, p: Caller, user_id: Annotated[str | None, Query(max_length=255)] = None
) -> dict[str, Any]:
    _require_global(p, Action.ACCESS_READ, "role-assignments")
    with _ctx(request).engine.connect() as conn:
        rows = conn.execute(_ASSIGNMENTS, {"u": user_id}).mappings().all()
    return {"items": [_assignment_json(r) for r in rows]}


def _entities(scope: str) -> tuple[str, ...]:
    return () if scope == GLOBAL else (scope,)


@router.post("/role-assignments", status_code=201, openapi_extra=access(Action.ACCESS_GRANT))
def grant(request: Request, p: Caller, body: GrantBody, idempotency_key: IdempotencyKey = None) -> Any:  # noqa: ARG001
    _require_global(p, Action.ACCESS_GRANT, "role-assignments")
    target = f"access:{body.user_id}:{body.role.value}:{body.scope}"
    if body.user_id == p.user_id:
        raise Denied(p.user_id, Action.ACCESS_GRANT.value, target=target, sod="nobody can grant a role to themselves")

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        inserted = (
            tx.execute(
                text(
                    "INSERT INTO role_assignments (assignment_id, user_id, role, scope, granted_by) "
                    "VALUES (:id, :u, :r, :s, :by) ON CONFLICT (user_id, role, scope) WHERE revoked_at IS NULL "
                    "DO NOTHING RETURNING assignment_id, user_id, role, scope, granted_by, granted_at"
                ),
                {"id": uuid.uuid4(), "u": body.user_id, "r": body.role.value, "s": body.scope, "by": p.user_id},
            )
            .mappings()
            .first()
        )
        if inserted is None:
            raise Problem(409, "The role is already granted", target, kind="conflict")
        event = audit.Event(p.user_id, "access.grant", entity_ids=_entities(body.scope), target=target)
        audit.append(tx, event, request_id_var.get())
        return 201, _assignment_json(inserted), f"assignment:{inserted['assignment_id']}"

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, body.model_dump(mode="json"), effect)


@router.post("/role-assignments/{assignment_id}:revoke", openapi_extra=access(Action.ACCESS_GRANT))
def revoke(request: Request, p: Caller, assignment_id: uuid.UUID, idempotency_key: IdempotencyKey = None) -> Any:  # noqa: ARG001
    _require_global(p, Action.ACCESS_GRANT, "role-assignments")

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        row = (
            tx.execute(
                text(
                    "UPDATE role_assignments SET revoked_by = :by, revoked_at = now() "
                    "WHERE assignment_id = :id AND revoked_at IS NULL "
                    "RETURNING assignment_id, user_id, role, scope, granted_by, granted_at"
                ),
                {"id": assignment_id, "by": p.user_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found(f"active assignment {assignment_id}")
        target = f"access:{row['user_id']}:{row['role']}:{row['scope']}"
        audit.append(tx, audit.Event(p.user_id, "access.revoke", entity_ids=_entities(row["scope"]), target=target))
        return 200, {**_assignment_json(row), "revoked_by": p.user_id}, f"assignment:{assignment_id}"

    return idempotency.run_once(_ctx(request).engine, request, p.user_id, {"assignment_id": str(assignment_id)}, effect)
