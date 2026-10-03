"""HTTP surface of the Jobs API emulator: the Jobs API 2.2 subset the close platform uses, plus the
discovery and OAuth endpoints the official ``databricks-sdk`` calls, so the SDK drives it unchanged.

Served:
  GET  /.well-known/databricks-config            host metadata (SDK startup probe)
  GET  /oidc/.well-known/oauth-authorization-server, POST /oidc/v1/token   (M2M OAuth, like an App)
  POST /api/2.2/jobs/run-now                      job_id, job_parameters, idempotency_token
  GET  /api/2.2/jobs/runs/get                     status (2.2) + legacy state + tasks[].run_id
  POST /api/2.2/jobs/runs/cancel
  GET  /api/2.2/jobs/runs/get-output              keyed by TASK run id, like Databricks
  GET  /api/2.2/jobs/runs/list                    job_id, start_time_from, active_only, paging
  GET  /api/2.2/jobs/get
Errors use Databricks' shape: {"error_code": ..., "message": ...} with a matching HTTP status.
Test-only fault switches live under /__emulator/ and exist only when EMULATOR_ALLOW_FAULTS=1:
  POST /__emulator/faults       {"mode": "normal" | "unavailable" | "hold-run-now", "seconds": N}
  POST /__emulator/revoke-tokens                 every OAuth access token issued so far stops working
  POST /__emulator/token-ttl    {"seconds": N}    idempotency-token memory
  POST /__emulator/job-timeout  {"job_id": J, "seconds": N}
  GET  /__emulator/runs         every run with its token and state (for test assertions)
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Form, Header, Request
from fastapi.responses import JSONResponse

from close_jobs_emulator.bundle import JobDefinition
from close_jobs_emulator.store import RunRow, RunStore

MAX_LOG_BYTES = 5 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


@dataclass
class EmulatorState:
    store: RunStore
    jobs: dict[int, JobDefinition]  # shared with the Runner, so a job-timeout fault reaches it
    pat: str
    client_id: str
    client_secret: str
    token_ttl_s: float = 3600.0
    allow_faults: bool = False
    unavailable: bool = False
    hold_run_now_s: float = 0.0  # fault: create the run, then hold the response (kill the caller meanwhile)
    token_epoch: int = 0  # fault: bumping it revokes every access token issued so far
    lock: threading.Lock = field(default_factory=threading.Lock)


TOKEN_LIFETIME_S = 3600


def _token_signature(state: EmulatorState, expires: int) -> str:
    key = f"{state.client_secret}:{state.token_epoch}".encode()
    return hmac.new(key, f"{state.client_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def mint_access_token(state: EmulatorState) -> str:
    """A self-contained signed token (like the workspace's JWTs), so it survives an emulator restart."""
    expires = int(time.time()) + TOKEN_LIFETIME_S
    return f"emu.{expires}.{_token_signature(state, expires)}"


def access_token_valid(state: EmulatorState, token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "emu" or not parts[1].isdigit():
        return False
    expires = int(parts[1])
    return expires > time.time() and hmac.compare_digest(parts[2], _token_signature(state, expires))


def _ms(t: float | None) -> int | None:
    return None if t is None else int(t * 1000)


def _state_blocks(r: RunRow) -> tuple[dict[str, Any], dict[str, Any]]:
    legacy: dict[str, Any] = {"life_cycle_state": r.life_cycle_state, "state_message": r.state_message}
    if r.result_state:
        legacy["result_state"] = r.result_state
    legacy["user_cancelled_or_timedout"] = r.result_state in {"CANCELED", "TIMEDOUT"}
    status: dict[str, Any] = {"state": r.state_v2}
    if r.state_v2 == "TERMINATED":
        status["termination_details"] = {
            "code": r.termination_code,
            "type": r.termination_type,
            "message": r.state_message,
        }
    return legacy, status


def run_json(r: RunRow, job: JobDefinition) -> dict[str, Any]:
    legacy, status = _state_blocks(r)
    params = {**job.defaults(), **r.job_parameters}
    return {
        "job_id": r.job_id,
        "run_id": r.run_id,
        "number_in_job": r.run_id,
        "run_name": job.name,
        "run_type": "JOB_RUN",
        "state": legacy,
        "status": status,
        "start_time": _ms(r.start_time or r.created_at),
        "end_time": _ms(r.end_time) or 0,
        "job_parameters": [
            {"name": p.name, "default": p.default, "value": params.get(p.name, p.default)} for p in job.parameters
        ],
        "tasks": [
            {
                "run_id": r.task_run_id,
                "task_key": job.task.task_key,
                "state": legacy,
                "status": status,
                "start_time": _ms(r.start_time),
                "end_time": _ms(r.end_time) or 0,
            }
        ],
    }


def _read_tail(path: str | None) -> tuple[str, bool]:
    if not path or not Path(path).exists():
        return "", False
    data = Path(path).read_bytes()
    truncated = len(data) > MAX_LOG_BYTES
    return data[-MAX_LOG_BYTES:].decode("utf-8", errors="replace"), truncated


def _job_of(state: EmulatorState, job_id: int) -> JobDefinition:
    job = state.jobs.get(job_id)
    if job is None:
        raise ApiError(404, "RESOURCE_DOES_NOT_EXIST", f"Job {job_id} does not exist.")
    return job


def job_json(job: JobDefinition) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "settings": {
            "name": job.name,
            "max_concurrent_runs": job.max_concurrent_runs,
            "timeout_seconds": job.timeout_seconds,
            "queue": {"enabled": job.queue_enabled},
            "parameters": [{"name": p.name, "default": p.default} for p in job.parameters],
            "tasks": [
                {
                    "task_key": job.task.task_key,
                    "max_retries": 0,
                    "python_wheel_task": {
                        "package_name": job.task.package_name,
                        "entry_point": job.task.entry_point,
                        "parameters": list(job.task.parameters),
                    },
                }
            ],
        },
    }


def run_now_json(state: EmulatorState, body: dict[str, Any]) -> dict[str, Any]:
    """``run-now``: a known idempotency token (within its TTL) returns the existing run."""
    job = _job_of(state, int(body.get("job_id", 0)))
    params = {str(k): str(v) for k, v in (body.get("job_parameters") or {}).items()}
    unknown = set(params) - {p.name for p in job.parameters}
    if unknown:
        raise ApiError(400, "INVALID_PARAMETER_VALUE", f"Unknown job parameters: {sorted(unknown)}")
    token_value = body.get("idempotency_token")
    if token_value is not None and not (isinstance(token_value, str) and 0 < len(token_value) <= 64):
        raise ApiError(400, "INVALID_PARAMETER_VALUE", "idempotency_token must be 1-64 characters")
    with state.lock:
        if token_value:
            existing = state.store.find_by_token(token_value, state.token_ttl_s)
            if existing is not None:
                return {"run_id": existing.run_id, "number_in_job": existing.run_id}
        row = state.store.create(job.job_id, params, token_value)
    if state.hold_run_now_s > 0:
        time.sleep(state.hold_run_now_s)
    return {"run_id": row.run_id, "number_in_job": row.run_id}


def output_json(state: EmulatorState, task_run_id: int) -> dict[str, Any]:
    """``runs/get-output`` is keyed by the task run id; a job run id is a 400, as on Databricks."""
    row = state.store.get_by_task(task_run_id)
    if row is None:
        if state.store.get(task_run_id) is not None:
            raise ApiError(
                400,
                "INVALID_PARAMETER_VALUE",
                "Retrieving the output of runs with multiple tasks is not supported. Use the task run id.",
            )
        raise ApiError(404, "RESOURCE_DOES_NOT_EXIST", f"Run {task_run_id} does not exist.")
    logs, truncated = _read_tail(row.stdout_path)
    out: dict[str, Any] = {
        "logs": logs,
        "logs_truncated": truncated,
        "metadata": run_json(row, _job_of(state, row.job_id)),
    }
    if row.state_v2 == "TERMINATED" and row.result_state not in {"SUCCESS", None}:
        err, _ = _read_tail(row.stderr_path)
        out["error"] = row.state_message
        out["error_trace"] = err[-20_000:]
    return out


def create_app(state: EmulatorState) -> FastAPI:
    app = FastAPI(title="close jobs emulator", docs_url=None, redoc_url=None)

    @app.exception_handler(ApiError)
    def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error_code": exc.code, "message": exc.message}, status_code=exc.status)

    app.include_router(_identity_routes(state))
    app.include_router(_jobs_routes(state))
    if state.allow_faults:
        app.include_router(_fault_routes(state))
    return app


def _identity_routes(state: EmulatorState) -> APIRouter:
    """Health, the SDK's host-metadata probe and the OIDC endpoints for M2M OAuth."""
    router = APIRouter()

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/.well-known/databricks-config")
    def host_metadata(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return {"oidc_endpoint": f"{base}/oidc", "workspace_id": "1001001"}

    @router.get("/oidc/.well-known/oauth-authorization-server")
    def oidc_discovery(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return {
            "issuer": f"{base}/oidc",
            "authorization_endpoint": f"{base}/oidc/v1/authorize",
            "token_endpoint": f"{base}/oidc/v1/token",
        }

    @router.post("/oidc/v1/token")
    def token(
        grant_type: str = Form(...),
        client_id: str | None = Form(default=None),
        client_secret: str | None = Form(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if authorization and authorization.startswith("Basic "):
            decoded = base64.b64decode(authorization[6:]).decode()
            client_id, _, client_secret = decoded.partition(":")
        if grant_type != "client_credentials" or (client_id, client_secret) != (state.client_id, state.client_secret):
            raise ApiError(401, "invalid_client", "invalid client credentials")
        with state.lock:
            issued = mint_access_token(state)
        return {"access_token": issued, "token_type": "Bearer", "expires_in": TOKEN_LIFETIME_S, "scope": "all-apis"}

    return router


def _jobs_routes(state: EmulatorState) -> APIRouter:
    """The Jobs API 2.2 subset the SDK uses (spike S6')."""
    router = APIRouter()

    def authenticated(authorization: str | None = Header(default=None)) -> None:
        if state.unavailable:
            raise ApiError(503, "TEMPORARILY_UNAVAILABLE", "the service is temporarily unavailable")
        token = (authorization or "").removeprefix("Bearer ").strip()
        with state.lock:
            valid = hmac.compare_digest(token, state.pat) or access_token_valid(state, token)
        if not token or not valid:
            raise ApiError(401, "UNAUTHENTICATED", "missing or invalid credentials")

    def job_of(job_id: int) -> JobDefinition:
        return _job_of(state, job_id)

    def run_of(run_id: int) -> RunRow:
        row = state.store.get(run_id)
        if row is None:
            raise ApiError(404, "RESOURCE_DOES_NOT_EXIST", f"Run {run_id} does not exist.")
        return row

    @router.post("/api/2.2/jobs/run-now", dependencies=[Depends(authenticated)])
    def run_now(body: dict[str, Any]) -> dict[str, Any]:
        return run_now_json(state, body)

    @router.get("/api/2.2/jobs/runs/get", dependencies=[Depends(authenticated)])
    def get_run(run_id: int) -> dict[str, Any]:
        row = run_of(run_id)
        return run_json(row, job_of(row.job_id))

    @router.post("/api/2.2/jobs/runs/cancel", dependencies=[Depends(authenticated)])
    def cancel_run(body: dict[str, Any]) -> dict[str, Any]:
        row = run_of(int(body.get("run_id", 0)))
        if row.state_v2 != "TERMINATED":
            state.store.update(row.run_id, cancel_requested=1)
        return {}

    @router.get("/api/2.2/jobs/runs/get-output", dependencies=[Depends(authenticated)])
    def get_output(run_id: int) -> dict[str, Any]:
        return output_json(state, run_id)

    @router.get("/api/2.2/jobs/runs/list", dependencies=[Depends(authenticated)])
    def list_runs(
        *,
        job_id: int | None = None,
        start_time_from: int | None = None,
        active_only: bool = False,
        limit: int = 25,
        page_token: str | None = None,
        expand_tasks: bool = False,
    ) -> dict[str, Any]:
        rows = state.store.list(job_id, None if start_time_from is None else start_time_from / 1000, active_only)
        offset = int(page_token or 0)
        page = rows[offset : offset + max(1, min(limit, 25))]
        runs = []
        for row in page:
            item = run_json(row, job_of(row.job_id))
            if not expand_tasks:
                item.pop("tasks", None)
            runs.append(item)
        result: dict[str, Any] = {"runs": runs, "has_more": offset + len(page) < len(rows)}
        if result["has_more"]:
            result["next_page_token"] = str(offset + len(page))
        return result

    @router.get("/api/2.2/jobs/get", dependencies=[Depends(authenticated)])
    def get_job(job_id: int) -> dict[str, Any]:
        return job_json(job_of(job_id))

    return router


def _fault_routes(state: EmulatorState) -> APIRouter:
    """Test-only knobs (EMULATOR_ALLOW_FAULTS=1), see the module docstring."""
    router = APIRouter()

    @router.post("/__emulator/faults")
    def set_faults(body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode", "normal")
        if mode not in {"normal", "unavailable", "hold-run-now"}:
            raise ApiError(400, "INVALID_PARAMETER_VALUE", f"unknown fault mode {mode!r}")
        state.unavailable = mode == "unavailable"
        state.hold_run_now_s = float(body.get("seconds", 30)) if mode == "hold-run-now" else 0.0
        return {"mode": mode, "unavailable": state.unavailable, "hold_run_now_s": state.hold_run_now_s}

    @router.post("/__emulator/revoke-tokens")
    def revoke_tokens() -> dict[str, Any]:
        with state.lock:
            state.token_epoch += 1
        return {"token_epoch": state.token_epoch}

    @router.post("/__emulator/job-timeout")
    def set_job_timeout(body: dict[str, Any]) -> dict[str, Any]:
        job = _job_of(state, int(body["job_id"]))
        state.jobs[job.job_id] = dataclasses.replace(job, timeout_seconds=int(body["seconds"]))
        return {"job_id": job.job_id, "timeout_seconds": state.jobs[job.job_id].timeout_seconds}

    @router.get("/__emulator/runs")
    def list_all_runs() -> list[dict[str, Any]]:
        return [
            {
                "run_id": r.run_id,
                "job_id": r.job_id,
                "idempotency_token": r.idempotency_token,
                "job_parameters": r.job_parameters,
                "state_v2": r.state_v2,
                "result_state": r.result_state,
                "termination_code": r.termination_code,
                "exit_code": r.exit_code,
                "start_time": r.start_time,
                "end_time": r.end_time,
            }
            for r in state.store.list(None, None, active_only=False)
        ]

    @router.post("/__emulator/token-ttl")
    def set_token_ttl(body: dict[str, Any]) -> dict[str, Any]:
        state.token_ttl_s = float(body["seconds"])
        return {"token_ttl_s": state.token_ttl_s}

    return router
