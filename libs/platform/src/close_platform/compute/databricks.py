"""ComputeBackend over the Databricks Jobs API 2.2, using the official ``databricks-sdk``.

The same code runs against Lakeflow Jobs in production and against the local Jobs API emulator, so
the client path is exercised on every local test run. Status mapping follows the 2.2 ``status``
object (``state`` + ``termination_details``); the legacy ``state`` field is only a fallback.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Final, override

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import Unauthenticated
from databricks.sdk.mixins.jobs import JobsExt

from close_contracts.enums import Env, JobStatus
from close_platform.compute.base import ComputeBackend, JobSpec, NotTerminalError, RunObservation

log = logging.getLogger(__name__)

RESULT_PREFIX: Final = "CLOSE_JOB_RESULT "
_ACTIVE_V2 = {"BLOCKED", "PENDING", "QUEUED", "RUNNING", "TERMINATING", "WAITING"}
_ACTIVE_V1 = {"PENDING", "QUEUED", "RUNNING", "TERMINATING", "BLOCKED", "WAITING_FOR_RETRY"}


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def observation_from_run(run: Any, external_run_id: int) -> RunObservation:
    """Map an SDK ``Run`` to a RunObservation. Anything unrecognised maps to UNKNOWN, never to terminal."""
    task_run_id = None
    tasks = getattr(run, "tasks", None) or []
    if tasks:
        task_run_id = getattr(tasks[0], "run_id", None)
    status = getattr(run, "status", None)
    state_v2 = _enum_name(getattr(status, "state", None)) if status is not None else None
    if state_v2 is not None:
        details = getattr(status, "termination_details", None)
        t_type = _enum_name(getattr(details, "type", None)) if details is not None else None
        t_code = _enum_name(getattr(details, "code", None)) if details is not None else None
        message = getattr(details, "message", None) if details is not None else None
        if state_v2 in _ACTIVE_V2:
            job_status = (
                JobStatus.PENDING if state_v2 in {"BLOCKED", "PENDING", "QUEUED", "WAITING"} else JobStatus.RUNNING
            )
        elif state_v2 == "TERMINATED":
            if t_code == "SUCCESS":
                job_status = JobStatus.SUCCEEDED
            elif t_code in {"CANCELED", "USER_CANCELED"}:
                job_status = JobStatus.CANCELLED
            else:
                job_status = JobStatus.FAILED
        else:
            job_status = JobStatus.UNKNOWN
        return RunObservation(job_status, external_run_id, state_v2, t_type, t_code, message, task_run_id)
    legacy = getattr(run, "state", None)
    life = _enum_name(getattr(legacy, "life_cycle_state", None)) if legacy is not None else None
    result = _enum_name(getattr(legacy, "result_state", None)) if legacy is not None else None
    message = getattr(legacy, "state_message", None) if legacy is not None else None
    if life in _ACTIVE_V1:
        job_status = JobStatus.RUNNING if life == "RUNNING" else JobStatus.PENDING
    elif life in {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}:
        job_status = {"SUCCESS": JobStatus.SUCCEEDED, "CANCELED": JobStatus.CANCELLED}.get(
            result or "", JobStatus.FAILED
        )
    else:
        job_status = JobStatus.UNKNOWN
    t_type = "INTERNAL_ERROR" if life == "INTERNAL_ERROR" else None
    return RunObservation(job_status, external_run_id, life, t_type, result, message, task_run_id)


def parse_result(logs: str | None) -> dict[str, object] | None:
    if not logs:
        return None
    lines = [ln for ln in logs.splitlines() if ln.startswith(RESULT_PREFIX)]
    if len(lines) != 1:
        return None
    parsed: dict[str, object] = json.loads(lines[0][len(RESULT_PREFIX) :])
    return parsed


class DatabricksJobsBackend(ComputeBackend):
    """``client`` may be a factory. Building a client resolves auth over the network, so the app passes a
    factory: it then starts (and serves reads) while the workspace is unreachable, the client is built
    on first use, and a failed build is simply retried by the next call."""

    def __init__(
        self,
        client: WorkspaceClient | Callable[[], WorkspaceClient],
        *,
        poll_interval: timedelta = timedelta(seconds=2),
    ) -> None:
        self._client = client if isinstance(client, WorkspaceClient) else None
        self._factory = None if isinstance(client, WorkspaceClient) else client
        self._lock = threading.Lock()
        self._poll = poll_interval.total_seconds()

    @property
    def _w(self) -> WorkspaceClient:
        if self._client is None:
            with self._lock:
                if self._client is None and self._factory is not None:
                    self._client = self._factory()
        if self._client is None:
            raise RuntimeError("no Jobs API client")
        return self._client

    @contextmanager
    def _jobs(self) -> Iterator[JobsExt]:
        """The Jobs API, dropping a client whose token was rejected. The SDK refreshes an OAuth token only
        when it expires, so after a revocation (or a secret rotation) the next call would otherwise fail
        until the old token runs out; with a factory the next call builds a client with a fresh token."""
        try:
            yield self._w.jobs
        except Unauthenticated:
            if self._factory is not None:
                with self._lock:
                    self._client = None
            raise

    def describe_job(self, job_id: int) -> str:
        """Readiness probe: the job exists and our credentials can read it."""
        with self._jobs() as jobs:
            job = jobs.get(job_id=job_id)
        name = job.settings.name if job.settings is not None else None
        return f"job {job.job_id} ({name})"

    @override
    def submit(self, spec: JobSpec, env: Env) -> int:
        with self._jobs() as jobs:
            waiter = jobs.run_now(
                job_id=spec.job_id,
                job_parameters=dict(spec.params),
                idempotency_token=spec.idempotency_token(env),
            )
        run_id: int = waiter.run_id
        return int(run_id)

    @override
    def find(self, spec: JobSpec, since: datetime) -> int | None:
        since_ms = int(since.timestamp() * 1000)
        with self._jobs() as jobs:
            for base_run in jobs.list_runs(job_id=spec.job_id, start_time_from=since_ms, expand_tasks=False):
                params = {p.name: p.value for p in (getattr(base_run, "job_parameters", None) or [])}
                if params.get("run_id") == spec.params.get("run_id") and params.get("attempt") == str(spec.attempt):
                    return int(base_run.run_id) if base_run.run_id is not None else None
        return None

    @override
    def observe(self, external_run_id: int) -> RunObservation:
        try:
            with self._jobs() as jobs:
                run = jobs.get_run(run_id=external_run_id)
        except Exception as exc:  # transport or API failure: nothing is known, nothing moves
            log.warning("jobs api observe failed: %s", exc)
            return RunObservation(JobStatus.UNKNOWN, external_run_id, message=f"{type(exc).__name__}: {exc}"[:500])
        return observation_from_run(run, external_run_id)

    @override
    def cancel(self, external_run_id: int) -> None:
        with self._jobs() as jobs:
            jobs.cancel_run(run_id=external_run_id)

    @override
    def result(self, observation: RunObservation) -> Mapping[str, object] | None:
        run_id = observation.task_run_id or observation.external_run_id
        with self._jobs() as jobs:
            output = jobs.get_run_output(run_id=run_id)
        return parse_result(getattr(output, "logs", None))

    @override
    def ensure_terminal(self, external_run_id: int, *, timeout: timedelta, cancel: bool) -> RunObservation:
        if cancel:
            self.cancel(external_run_id)
        deadline = time.monotonic() + timeout.total_seconds()
        while True:
            obs = self.observe(external_run_id)
            if obs.status.is_terminal:
                return obs
            if time.monotonic() >= deadline:
                raise NotTerminalError(f"run {external_run_id} not confirmed terminal ({obs.status}) within {timeout}")
            time.sleep(self._poll)
