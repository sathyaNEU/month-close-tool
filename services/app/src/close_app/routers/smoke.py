"""Smoke runs: the Stage 1 end-to-end probe through the whole platform, and whoami.

POST starts a run in one transaction (idempotency key, writer lease, run, attempt, outbox event);
the outbox relay submits it and the poller finishes it. GET returns the run; once it has succeeded,
its per-entity result is read back from the lake (``ops.run_manifests``) through the read path, bound
to the run's attempt and fencing token and scoped to the caller's entities. ``/rows`` pages through the
run's rows and ``/totals`` groups them, both as typed QuerySpecs over Spark Connect.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection

from close_app import audit, idempotency
from close_app.authz import Action, access
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.guard import Caller, require_anywhere
from close_app.identity import Principal
from close_app.logs import request_id_var
from close_contracts.query import Aggregate, Eq, Filter, Prefix, QueryError, QuerySpec, Range, Sort
from close_contracts.query_cursor import CursorError
from close_contracts.queryable import RUN_MANIFESTS, SMOKE_RESULTS
from close_platform.leases import LeaseBusyError
from close_platform.query import Caller as ReadCaller
from close_platform.query import Page, QueryBusyError, QueryTimeoutError
from close_platform.runs import Attempt, Run, RunState

router = APIRouter(prefix="/api/v1")
LEASE = "smoke:default"


class SmokeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: int = Field(default=1000, ge=1, le=10_000_000)


@router.get("/whoami", openapi_extra=access("authenticated"))
def whoami(principal: Caller) -> dict[str, Any]:
    return {
        "user_id": principal.user_id,
        "email": principal.email,
        "display_name": principal.display_name,
        "entities": sorted(principal.entities),
        "roles": sorted([role, scope] for role, scope in principal.assignments),
        "request_id": request_id_var.get(),
    }


@router.post("/smoke/runs", status_code=202, openapi_extra=access(Action.SMOKE_RUN))
def start_smoke_run(
    request: Request,
    body: SmokeRunRequest,
    principal: Caller,
    idempotency_key: Annotated[str | None, Header()] = None,  # noqa: ARG001 (read by run_once; declared for the API)
) -> idempotency.CanonicalJSONResponse:
    ctx: AppContext = request.app.state.ctx
    require_anywhere(principal, Action.SMOKE_RUN, target="smoke")

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        try:
            started = ctx.coordinator.start(
                tx,
                job_key="smoke",
                lease_resource=LEASE,
                requested_by=principal.user_id,
                request_id=request_id_var.get(),
                params={"rows": body.rows},
            )
        except LeaseBusyError as busy:
            holder = busy.holder
            raise Problem(
                409,
                "The smoke writer lease is held by another run",
                "retry after it finishes (or after its status becomes known again)",
                kind="conflict",
                holder_run_id=str(holder.run_id) if holder else None,
            ) from None
        response = {
            "run_id": str(started.run.run_id),
            "state": started.run.state.value,
            "attempt": started.attempt.attempt,
            "fencing_token": started.grant.fencing_token,
        }
        event = audit.Event(
            principal.user_id, "smoke.run", target=f"run:{started.run.run_id}", payload=body.model_dump()
        )
        audit.append(tx, event, request_id_var.get())  # the last lock this transaction takes
        return 202, response, f"run:{started.run.run_id}"

    return idempotency.run_once(ctx.engine, request, principal.user_id, body.model_dump(), effect)


def _visible_result(result: dict[str, Any] | None, principal: Principal) -> dict[str, Any] | None:
    if result is None:
        return None
    digests = result.get("digests_by_entity") or {}
    rows = result.get("rows_by_entity") or {}
    visible = sorted(principal.entities & set(digests))
    return {
        "entities": visible,
        "rows": sum(int(rows.get(e, 0)) for e in visible),
        "digests_by_entity": {e: digests[e] for e in visible},
        "fenced": bool(result.get("fenced")),
        "spark_master": result.get("spark_master"),
        "engine_version": result.get("engine_version"),
    }


def run_query(ctx: AppContext, spec: QuerySpec, principal: Principal, cursor: str | None = None) -> Page:
    if ctx.query is None:
        raise Problem(503, "The read path is not configured")
    try:
        return ctx.query.run(spec, ReadCaller(principal.user_id, principal.entities), cursor)
    except CursorError as exc:
        raise Problem(400, "Invalid cursor", str(exc)) from None
    except QueryError as exc:
        raise Problem(400, "Invalid query", str(exc)) from None
    except QueryBusyError:
        raise Problem(503, "The read path is busy", "retry shortly") from None
    except QueryTimeoutError:
        raise Problem(504, "The query timed out") from None


def _manifest_result(ctx: AppContext, run: Run, attempt: Attempt, principal: Principal) -> dict[str, Any]:
    """The run's per-entity result from the lake. Only the manifest written by this attempt with this
    fencing token counts, so a row from a residual-window zombie can never surface; identical repeats
    are merged, and two different digests for one entity are an integrity error."""
    spec = QuerySpec(
        RUN_MANIFESTS.key,
        columns=("run_id", "attempt", "fencing_token", "entity_id", "row_count", "digest"),
        filters=(
            Eq("run_id", run.run_id.hex),
            Eq("attempt", attempt.attempt),
            Eq("fencing_token", attempt.fencing_token),
        ),
        sort=tuple(Sort(c) for c in RUN_MANIFESTS.unique_key),
        limit=500,
    )
    by_entity: dict[str, tuple[int, str]] = {}
    for row in run_query(ctx, spec, principal).rows:
        value = (int(str(row["row_count"])), str(row["digest"]))
        if by_entity.setdefault(str(row["entity_id"]), value) != value:
            raise Problem(500, "Result integrity error", f"two different manifests for entity {row['entity_id']}")
    visible = sorted(by_entity)
    reported = run.result or {}
    return {
        "entities": visible,
        "rows": sum(by_entity[e][0] for e in visible),
        "digests_by_entity": {e: by_entity[e][1] for e in visible},
        "fenced": bool(reported.get("fenced")),
        "spark_master": reported.get("spark_master"),
        "engine_version": reported.get("engine_version"),
    }


def _smoke_run(ctx: AppContext, run_id: UUID) -> tuple[Run, Attempt | None]:
    with ctx.engine.connect() as conn:
        run = ctx.coordinator.runs.get_run(conn, run_id)
        if run is None or run.job_key != "smoke":
            raise Problem(404, "Run not found")
        attempt = ctx.coordinator.runs.get_attempt(conn, run_id, run.current_attempt)
    return run, attempt


def _succeeded(ctx: AppContext, run_id: UUID) -> Run:
    run, attempt = _smoke_run(ctx, run_id)
    if run.state != RunState.SUCCEEDED or attempt is None:
        raise Problem(409, "Results are available once the run has succeeded", f"the run is {run.state.value}")
    return run


@router.get("/smoke/runs/{run_id}", openapi_extra=access("authenticated"))
def get_smoke_run(request: Request, run_id: UUID, principal: Caller) -> dict[str, Any]:
    ctx: AppContext = request.app.state.ctx
    run, attempt = _smoke_run(ctx, run_id)
    result: dict[str, Any] | None
    if run.state == RunState.SUCCEEDED and attempt is not None:
        result = _manifest_result(ctx, run, attempt, principal)  # Spark Connect only once there is a result
    else:
        result = _visible_result(run.result, principal)
    return {
        "run_id": str(run.run_id),
        "state": run.state.value,
        "attempt": run.current_attempt,
        "fencing_token": attempt.fencing_token if attempt else None,
        "external_run_id": attempt.external_run_id if attempt else None,
        "requested_by": run.requested_by,
        "created_at": run.created_at.isoformat(),
        "terminal_at": run.terminal_at.isoformat() if run.terminal_at else None,
        "error": run.error,
        "result": result,
    }


ROW_COLUMNS = ("row_id", "entity_id", "currency", "amount_minor", "amount_eur_minor", "seq")


@router.get("/smoke/runs/{run_id}/rows", openapi_extra=access("authenticated"))
def smoke_rows(
    request: Request,
    run_id: UUID,
    principal: Caller,
    *,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
    currency: Annotated[str | None, Query(max_length=16)] = None,
    min_amount: int | None = None,
    max_amount: int | None = None,
    row_prefix: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
) -> dict[str, Any]:
    """The run's rows, largest amount first. A cursor continues the same query: send the same filters
    and page size with it."""
    ctx: AppContext = request.app.state.ctx
    _succeeded(ctx, run_id)
    filters: list[Filter] = [Eq("run_id", run_id.hex)]
    if currency is not None:
        filters.append(Eq("currency", currency))
    if min_amount is not None or max_amount is not None:
        filters.append(Range("amount_minor", min_amount, max_amount))
    if row_prefix is not None:
        filters.append(Prefix("row_id", row_prefix))
    spec = QuerySpec(
        SMOKE_RESULTS.key, ROW_COLUMNS, tuple(filters), (Sort("amount_minor", True), Sort("row_id")), limit=page_size
    )
    page = run_query(ctx, spec, principal, cursor)
    return {"rows": list(page.rows), "next_cursor": page.next_cursor, "version": page.version}


@router.get("/smoke/runs/{run_id}/totals", openapi_extra=access("authenticated"))
def smoke_totals(request: Request, run_id: UUID, principal: Caller) -> dict[str, Any]:
    """Row counts and amount totals per entity and currency, for the caller's entities."""
    ctx: AppContext = request.app.state.ctx
    _succeeded(ctx, run_id)
    spec = QuerySpec(
        SMOKE_RESULTS.key,
        filters=(Eq("run_id", run_id.hex),),
        group_by=("entity_id", "currency"),
        aggregates=(Aggregate("count", "rows"), Aggregate("sum", "amount_minor_total", "amount_minor")),
        limit=1000,
    )
    page = run_query(ctx, spec, principal)
    return {"groups": list(page.rows), "version": page.version}


@router.post("/smoke/runs/{run_id}:cancel", status_code=202, openapi_extra=access(Action.SMOKE_RUN))
def cancel_smoke_run(
    request: Request,
    run_id: UUID,
    principal: Caller,
    idempotency_key: Annotated[str | None, Header()] = None,  # noqa: ARG001 (read by run_once; declared for the API)
) -> idempotency.CanonicalJSONResponse:
    ctx: AppContext = request.app.state.ctx
    require_anywhere(principal, Action.SMOKE_RUN, target=f"run:{run_id}")

    def effect(tx: Connection) -> tuple[int, dict[str, Any], str | None]:
        run = ctx.coordinator.runs.get_run(tx, run_id)
        if run is None or run.job_key != "smoke":
            raise Problem(404, "Run not found", kind="not-found")
        if not ctx.coordinator.request_cancel(tx, run_id):
            raise Problem(409, "Run is not active", f"the run is {run.state.value}", kind="conflict")
        audit.append(tx, audit.Event(principal.user_id, "smoke.cancel", target=f"run:{run_id}"), request_id_var.get())
        return 202, {"run_id": str(run_id), "state": "cancel_requested", "requested_by": principal.user_id}, None

    return idempotency.run_once(ctx.engine, request, principal.user_id, {"run_id": str(run_id)}, effect)
