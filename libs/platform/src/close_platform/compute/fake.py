"""In-memory ComputeBackend for unit tests: scripted run lifecycles, idempotency, outages."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import override

from close_contracts.enums import Env, JobStatus
from close_platform.compute.base import ComputeBackend, JobSpec, NotTerminalError, RunObservation


@dataclass
class FakeRun:
    spec: JobSpec
    token: str
    status: JobStatus = JobStatus.PENDING
    termination_type: str | None = None
    termination_code: str | None = None
    result: dict[str, object] | None = None
    started_at: datetime | None = None


@dataclass
class FakeComputeBackend(ComputeBackend):
    """Tests drive runs with ``advance``; ``unavailable`` simulates a Jobs API outage."""

    unavailable: bool = False
    fail_submit: bool = False
    runs: dict[int, FakeRun] = field(default_factory=dict)
    by_token: dict[str, int] = field(default_factory=dict)
    submit_calls: int = 0
    cancel_calls: int = 0
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1000))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @override
    def submit(self, spec: JobSpec, env: Env) -> int:
        with self._lock:
            self.submit_calls += 1
            if self.unavailable or self.fail_submit:
                raise ConnectionError("jobs api unavailable")
            token = spec.idempotency_token(env)
            if token in self.by_token:
                return self.by_token[token]
            run_id = next(self._ids)
            self.runs[run_id] = FakeRun(spec, token)
            self.by_token[token] = run_id
            return run_id

    @override
    def find(self, spec: JobSpec, since: datetime) -> int | None:
        for run_id, run in self.runs.items():
            if run.spec.run_id == spec.run_id and run.spec.attempt == spec.attempt:
                return run_id
        return None

    def advance(
        self,
        run_id: int,
        status: JobStatus,
        *,
        result: Mapping[str, object] | None = None,
        termination_type: str | None = None,
        termination_code: str | None = None,
    ) -> None:
        run = self.runs[run_id]
        run.status = status
        run.result = None if result is None else dict(result)
        run.termination_type = termination_type
        run.termination_code = termination_code or {
            JobStatus.SUCCEEDED: "SUCCESS",
            JobStatus.FAILED: "RUN_EXECUTION_ERROR",
            JobStatus.CANCELLED: "CANCELED",
        }.get(status)

    @override
    def observe(self, external_run_id: int) -> RunObservation:
        if self.unavailable:
            return RunObservation(JobStatus.UNKNOWN, external_run_id, message="unavailable")
        run = self.runs[external_run_id]
        raw = "TERMINATED" if run.status.is_terminal else run.status.value
        return RunObservation(
            run.status, external_run_id, raw, run.termination_type, run.termination_code, None, external_run_id
        )

    @override
    def cancel(self, external_run_id: int) -> None:
        self.cancel_calls += 1
        if self.unavailable:
            raise ConnectionError("jobs api unavailable")
        run = self.runs[external_run_id]
        if not run.status.is_terminal:
            run.status = JobStatus.CANCELLED
            run.termination_code = "CANCELED"

    @override
    def result(self, observation: RunObservation) -> Mapping[str, object] | None:
        return self.runs[observation.external_run_id].result

    @override
    def ensure_terminal(self, external_run_id: int, *, timeout: timedelta, cancel: bool) -> RunObservation:
        if cancel:
            self.cancel(external_run_id)
        obs = self.observe(external_run_id)
        if not obs.status.is_terminal:
            raise NotTerminalError(f"run {external_run_id} is {obs.status}")
        return obs
