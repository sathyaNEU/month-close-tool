"""Liveness and readiness. Readiness fails unless the schema is at the expected head and every
dependency check passes, so a rolling deploy never routes traffic to an instance that cannot work."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from close_app.authz import access
from close_app.context import AppContext

router = APIRouter()


@router.get("/healthz", openapi_extra=access("public"))
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", openapi_extra=access("public"))
def readyz(request: Request) -> JSONResponse:
    ctx: AppContext = request.app.state.ctx
    results = {}
    ready = True
    for name, check in ctx.readiness:
        try:
            ok, detail = check()
        except Exception as exc:  # a failing dependency makes the instance not ready, never crashes it
            ok, detail = False, f"{type(exc).__name__}: {exc}"[:300]
        results[name] = {"ok": ok, "detail": detail}
        ready = ready and ok
    return JSONResponse({"ready": ready, "checks": results}, status_code=200 if ready else 503)
