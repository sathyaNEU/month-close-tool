"""RFC 9457 problem details for every error response (unexpected failures: see ``middleware``)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from close_app.logs import request_id_var

PROBLEM_JSON = "application/problem+json"


PROBLEM_TYPES = frozenset(
    {
        "forbidden",
        "not-found",
        "sod-violation",
        "precondition-failed",
        "precondition-required",
        "validation-failed",
        "conflict",
    }
)


def problem_type(slug: str | None) -> str:
    """A stable problem type URI (RFC 9457), or ``about:blank`` when the status says it all."""
    if slug is None:
        return "about:blank"
    if slug not in PROBLEM_TYPES:
        raise ValueError(f"unknown problem type {slug}")
    return f"urn:close:problem:{slug}"


class Problem(Exception):  # noqa: N818 (the RFC 9457 name)
    def __init__(
        self, status: int, title: str, detail: str | None = None, *, kind: str | None = None, **extra: Any
    ) -> None:
        self.status, self.title, self.detail, self.extra = status, title, detail, extra
        self.type = problem_type(kind)
        super().__init__(title)


class Denied(Problem):
    """A refused privileged action (403, or 409 for separation of duties). The refusal itself is audited,
    in its own transaction, because the request's transaction has rolled back."""

    def __init__(
        self,
        actor: str,
        action: str,
        *,
        target: str | None,
        entity_ids: tuple[str, ...] = (),
        sod: str | None = None,
    ) -> None:
        detail = sod or f"{action} is not granted to you here"
        status, kind = (409, "sod-violation") if sod else (403, "forbidden")
        super().__init__(status, "Separation of duties" if sod else "Forbidden", detail, kind=kind)
        self.actor, self.action, self.target, self.entity_ids, self.sod = actor, action, target, entity_ids, sod


def problem_response(
    request: Request,
    status: int,
    title: str,
    detail: str | None = None,
    *,
    type_uri: str = "about:blank",
    **extra: Any,
) -> JSONResponse:
    body = {
        "type": type_uri,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "request_id": request_id_var.get(),
        **extra,
    }
    return JSONResponse({k: v for k, v in body.items() if v is not None}, status_code=status, media_type=PROBLEM_JSON)


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    def _problem(request: Request, exc: Problem) -> JSONResponse:
        if isinstance(exc, Denied):
            _audit_denial(request, exc)
        return problem_response(request, exc.status, exc.title, exc.detail, type_uri=exc.type, **exc.extra)

    @app.exception_handler(HTTPException)
    def _http(request: Request, exc: HTTPException) -> JSONResponse:
        return problem_response(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return problem_response(
            request, 422, "Request validation failed", type_uri=problem_type("validation-failed"), errors=errors
        )


def _audit_denial(request: Request, exc: Denied) -> None:
    from close_app import audit  # a late import: audit needs the database layer, errors does not

    event = audit.Event(
        actor=exc.actor,
        action=exc.action,
        outcome="denied",
        entity_ids=exc.entity_ids,
        target=exc.target,
        payload={"reason": exc.detail or "", "method": request.method, "path": request.url.path},
    )
    with request.app.state.ctx.engine.begin() as tx:
        audit.append(tx, event, request_id_var.get())
