"""Per-request context: the request id, one JSON access line, and a problem response for any error
nothing else handled.

Unexpected errors are caught here rather than by an ``Exception`` handler. Starlette runs that
handler in its outermost middleware, after this one has already reset the request id, so the 500
body and its log line would lose the id that ties them together.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from close_app.errors import problem_response
from close_app.logs import request_id_var

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_access = logging.getLogger("close_app.access")
_errors = logging.getLogger("close_app.errors")


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        raw = request.headers.get("x-request-id", "")
        token = request_id_var.set(raw if _REQUEST_ID_RE.fullmatch(raw) else None)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # the 500 carries no detail; the log line has it, with the same id
                _errors.error("unhandled error", exc_info=exc)
                response = problem_response(request, 500, "Internal error")
            _access.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            return response
        finally:
            request_id_var.reset(token)
