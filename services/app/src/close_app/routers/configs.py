"""Configuration versions over HTTP: ``/api/v1/data-sources`` and ``/api/v1/match-sets`` (roadmap Backend API).

    GET  /{resource}                                   configurations the caller may read (latest versions)
    POST /{resource}                                   version 1 of a new configuration (a draft)
    GET  /{resource}/{id}/versions                     its versions
    POST /{resource}/{id}/versions                     the next version (a draft)
    GET  /{resource}/{id}/versions/{v}                 one version, with its ETag
    PUT  /{resource}/{id}/versions/{v}                 edit a draft (If-Match)
    POST /{resource}/{id}/versions/{v}:{verb}          submit, approve, reject, activate, retire (If-Match)

- **Data sources and the journaling configurations** are global: authoring, approving and activating one needs the
  action at ``GLOBAL``, and any principal holding ``config.read`` anywhere may read them.
- **Match sets and recurring journals** belong to their entities: reading one needs ``config.read`` in any one of
  them, every other action needs the role in all of them, and one in none of the caller's entities is a 404.
- **POSTs** go through ``Idempotency-Key``. **Edits and transitions** need ``If-Match``, which is checked after the
  idempotency lookup, so a replay after a later change still returns the stored response.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, RowMapping

from close_app import configs, idempotency
from close_app.authz import Action, access, allowed, allowed_any
from close_app.context import AppContext
from close_app.errors import Denied, Problem
from close_app.guard import Caller, not_found, require, require_anywhere
from close_app.identity import Principal
from close_app.logs import request_id_var
from close_contracts.config.envelope import ConfigKind

router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header()]
IfMatch = Annotated[str | None, Header()]
VERB_ACTION = {
    "submit": Action.CONFIG_AUTHOR,
    "approve": Action.CONFIG_APPROVE,
    "reject": Action.CONFIG_APPROVE,
    "activate": Action.CONFIG_ACTIVATE,
    "retire": Action.CONFIG_ACTIVATE,
}


class VersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective_from: date
    spec: dict[str, Any]


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, min_length=1, max_length=2000)  # reject
    effective_to: date | None = None  # retire


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _guard(p: Principal, kind: ConfigKind, action: Action, entities: tuple[str, ...] | None, what: str) -> None:
    if kind not in configs.ENTITY_SCOPED and action == Action.CONFIG_READ:  # global configurations
        require_anywhere(p, action, target=what)
    elif kind not in configs.ENTITY_SCOPED:
        require(p, action, None, target=what)
    else:
        require(p, action, entities, target=what, every=action != Action.CONFIG_READ)


def _author_new(p: Principal, kind: ConfigKind, parsed: configs.Parsed) -> None:
    """Creating needs the author role where the new version will live; nothing exists yet to hide."""
    if not allowed(p.assignments, Action.CONFIG_AUTHOR, parsed.entities):
        raise Denied(
            p.user_id,
            Action.CONFIG_AUTHOR.value,
            target=f"{kind.value}:{parsed.config_id}",
            entity_ids=parsed.entities or (),
        )


def _check_if_match(request: Request, row: RowMapping) -> None:
    values = request.headers.getlist("if-match")
    if not values:
        raise Problem(428, "If-Match is required", "send the version's ETag", kind="precondition-required")
    sent = {v.strip() for value in values for v in value.split(",")}
    current = configs.etag(row)
    if current not in sent:
        raise Problem(412, "The version has changed", "reload it", kind="precondition-failed", current_etag=current)


def _found(tx: Connection, kind: ConfigKind, config_id: str, version: int, *, lock: bool = False) -> RowMapping:
    row = configs.load(tx, kind, config_id, version, lock=lock)
    if row is None:
        raise not_found(f"{kind.value}:{config_id}:v{version}")
    return row


def _register(resource: str, kind: ConfigKind) -> None:
    base = f"/{resource}"

    @router.get(base, openapi_extra=access(Action.CONFIG_READ), name=f"list_{kind.value}")
    def list_configs(request: Request, p: Caller) -> dict[str, Any]:
        if kind not in configs.ENTITY_SCOPED:
            require_anywhere(p, Action.CONFIG_READ, target=resource)
        with _ctx(request).engine.connect() as conn:
            rows = configs.latest_per_config(conn, kind)
        visible = [
            r
            for r in rows
            if kind not in configs.ENTITY_SCOPED or allowed_any(p.assignments, Action.CONFIG_READ, configs.entities(r))
        ]
        return {"items": [configs.as_json(r, with_spec=False) for r in visible]}

    def create_version(request: Request, p: Caller, body: VersionBody, config_id: str | None) -> Any:
        ctx = _ctx(request)
        parsed = configs.parse(kind, body.spec)
        if config_id is not None and parsed.config_id != config_id:
            raise Problem(422, "The id in the path and the specification differ", kind="validation-failed")
        _author_new(p, kind, parsed)

        def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
            row = configs.create(
                tx, kind, parsed, body.effective_from, actor=p.user_id, request_id=request_id_var.get()
            )
            return 201, configs.as_json(row), configs.target(row)

        return idempotency.run_once(ctx.engine, request, p.user_id, body.model_dump(mode="json"), effect)

    @router.post(base, status_code=201, openapi_extra=access(Action.CONFIG_AUTHOR), name=f"create_{kind.value}")
    def create(request: Request, p: Caller, body: VersionBody, idempotency_key: IdempotencyKey = None) -> Any:  # noqa: ARG001
        return create_version(request, p, body, None)

    @router.get(
        f"{base}/{{config_id}}/versions", openapi_extra=access(Action.CONFIG_READ), name=f"versions_{kind.value}"
    )
    def list_versions(request: Request, p: Caller, config_id: str) -> dict[str, Any]:
        with _ctx(request).engine.connect() as conn:
            rows = configs.versions(conn, kind, config_id)
        if not rows:
            raise not_found(f"{kind.value}:{config_id}")
        _guard(p, kind, Action.CONFIG_READ, configs.entities(rows[-1]), f"{kind.value}:{config_id}")
        return {"items": [configs.as_json(r, with_spec=False) for r in rows]}

    @router.post(
        f"{base}/{{config_id}}/versions",
        status_code=201,
        openapi_extra=access(Action.CONFIG_AUTHOR),
        name=f"new_version_{kind.value}",
    )
    def new_version(
        request: Request,
        p: Caller,
        config_id: str,
        body: VersionBody,
        idempotency_key: IdempotencyKey = None,  # noqa: ARG001
    ) -> Any:
        return create_version(request, p, body, config_id)

    @router.get(
        f"{base}/{{config_id}}/versions/{{version}}", openapi_extra=access(Action.CONFIG_READ), name=f"get_{kind.value}"
    )
    def get_version(request: Request, p: Caller, config_id: str, version: int) -> JSONResponse:
        with _ctx(request).engine.connect() as conn:
            row = _found(conn, kind, config_id, version)
        _guard(p, kind, Action.CONFIG_READ, configs.entities(row), configs.target(row))
        return JSONResponse(configs.as_json(row), headers={"ETag": configs.etag(row)})

    @router.put(
        f"{base}/{{config_id}}/versions/{{version}}",
        openapi_extra=access(Action.CONFIG_AUTHOR),
        name=f"edit_{kind.value}",
    )
    def edit_version(
        request: Request,
        p: Caller,
        config_id: str,
        version: int,
        *,
        body: VersionBody,
        if_match: IfMatch = None,  # noqa: ARG001 (read by _check_if_match; declared for the API)
    ) -> JSONResponse:
        parsed = configs.parse(kind, body.spec)
        with _ctx(request).engine.begin() as tx:
            row = _found(tx, kind, config_id, version, lock=True)
            _guard(p, kind, Action.CONFIG_AUTHOR, configs.entities(row), configs.target(row))
            _author_new(p, kind, parsed)
            _check_if_match(request, row)
            new = configs.edit(tx, row, parsed, body.effective_from, actor=p.user_id, request_id=request_id_var.get())
        return JSONResponse(configs.as_json(new), headers={"ETag": configs.etag(new)})

    for verb, action in VERB_ACTION.items():
        _register_transition(base, kind, verb, action)


def _register_transition(base: str, kind: ConfigKind, verb: str, action: Action) -> None:
    @router.post(
        f"{base}/{{config_id}}/versions/{{version:int}}:{verb}",
        openapi_extra=access(action),
        name=f"{verb}_{kind.value}",
    )
    def step(
        request: Request,
        p: Caller,
        config_id: str,
        version: int,
        *,
        body: TransitionBody | None = None,
        idempotency_key: IdempotencyKey = None,  # noqa: ARG001
        if_match: IfMatch = None,  # noqa: ARG001
    ) -> Any:
        payload = (body or TransitionBody()).model_dump(mode="json")

        def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
            row = _found(tx, kind, config_id, version, lock=True)
            entities = configs.entities(row)
            _guard(p, kind, action, entities, configs.target(row))
            _check_if_match(request, row)
            violation = configs.sod_violation(row, verb, p.user_id)
            if violation is not None:
                raise Denied(
                    p.user_id, action.value, target=configs.target(row), entity_ids=entities or (), sod=violation
                )
            if verb == "reject" and not payload["reason"]:
                raise Problem(422, "A rejection needs a reason", kind="validation-failed")
            new = configs.transition(
                tx,
                row,
                verb,
                actor=p.user_id,
                request_id=request_id_var.get(),
                reason=payload["reason"],
                effective_to=date.fromisoformat(payload["effective_to"]) if payload["effective_to"] else None,
            )
            return 200, configs.as_json(new), configs.target(new)

        return idempotency.run_once(_ctx(request).engine, request, p.user_id, payload, effect)


for _resource, _kind in configs.RESOURCES.items():
    _register(_resource, _kind)
