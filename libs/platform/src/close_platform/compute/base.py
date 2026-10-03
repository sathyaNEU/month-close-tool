"""Compute backends: how the control plane launches and watches engine jobs.

Production and local development use the same Databricks Jobs API client (ADR-0006, S6'): in
production it talks to Lakeflow Jobs, locally to the Jobs API emulator. Tests use the in-memory fake.

Safety rules the implementations must keep:

* ``submit`` is idempotent for the same ``JobSpec`` (same idempotency token -> same external run).
* ``observe`` never raises for transport problems; it returns ``JobStatus.UNKNOWN`` instead, and the
  caller must treat UNKNOWN as "nothing is known", never as finished.
* ``ensure_terminal`` never assumes a run finished: on timeout or UNKNOWN it raises NotTerminalError.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol
from uuid import UUID

from close_contracts.enums import Env, JobStatus

JOB_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,13}$")
MAX_TOKEN_LEN: Final = 64


class NotTerminalError(RuntimeError):
    """A run could not be confirmed terminal (timeout or unknown status)."""


@dataclass(frozen=True)
class JobSpec:
    job_key: str
    job_id: int
    run_id: UUID
    attempt: int
    params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not JOB_KEY_RE.fullmatch(self.job_key):
            raise ValueError(f"invalid job key {self.job_key!r} (1-14 chars, ^[a-z][a-z0-9_]*$)")
        if not 1 <= self.attempt <= 99:
            raise ValueError(f"attempt {self.attempt} out of range 1..99")
        for key, value in self.params.items():
            if not isinstance(value, str):
                raise TypeError(f"job parameter {key!r} must be a string (Jobs API job_parameters)")

    def idempotency_token(self, env: Env) -> str:
        token = f"close-{env.value}-{self.job_key}-{self.run_id.hex}-a{self.attempt:02d}"
        if len(token) > MAX_TOKEN_LEN:
            raise ValueError(f"idempotency token too long ({len(token)} > {MAX_TOKEN_LEN})")
        return token


@dataclass(frozen=True)
class RunObservation:
    """What the Jobs API says about one external run (a snapshot, stored as the attempt's last status)."""

    status: JobStatus
    external_run_id: int
    raw_state: str | None = None
    termination_type: str | None = None
    termination_code: str | None = None
    message: str | None = None
    task_run_id: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "external_run_id": self.external_run_id,
            "raw_state": self.raw_state,
            "termination_type": self.termination_type,
            "termination_code": self.termination_code,
            "message": self.message,
            "task_run_id": self.task_run_id,
        }


class ComputeBackend(Protocol):
    def submit(self, spec: JobSpec, env: Env) -> int:
        """Start the job (or return the run already started with the same idempotency token)."""
        ...

    def find(self, spec: JobSpec, since: datetime) -> int | None:
        """Look for a run of this attempt started after ``since`` (used after the token window)."""
        ...

    def observe(self, external_run_id: int) -> RunObservation: ...

    def cancel(self, external_run_id: int) -> None: ...

    def result(self, observation: RunObservation) -> Mapping[str, object] | None:
        """The job's parsed ``CLOSE_JOB_RESULT`` line, if the run produced one."""
        ...

    def ensure_terminal(self, external_run_id: int, *, timeout: timedelta, cancel: bool) -> RunObservation: ...
