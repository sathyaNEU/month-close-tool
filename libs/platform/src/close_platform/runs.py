"""Runs registry: the durable state machine for every job the platform launches.

A run is the business request ("run matching for match set X"); an attempt is one submission of
it to the Jobs API. Every state change is a compare-and-set on the expected state, so any number of
app instances (and retries) can poll and deliver concurrently without double-transitions.

Run states:     requested -> submitted -> running -> succeeded | failed | cancelled
                (any active) -> cancel_requested -> cancelled | succeeded | failed
                requested -> submit_failed
Attempt states: pending -> submitted -> running -> terminated;  pending -> submit_failed
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


class RunState(StrEnum):
    REQUESTED = "requested"
    SUBMITTED = "submitted"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUBMIT_FAILED = "submit_failed"


ACTIVE_RUN_STATES = frozenset({RunState.REQUESTED, RunState.SUBMITTED, RunState.RUNNING, RunState.CANCEL_REQUESTED})
TERMINAL_RUN_STATES = frozenset(set(RunState) - ACTIVE_RUN_STATES)


class AttemptState(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    RUNNING = "running"
    TERMINATED = "terminated"
    SUBMIT_FAILED = "submit_failed"


@dataclass(frozen=True)
class Run:
    run_id: UUID
    job_key: str
    lease_resource: str
    requested_by: str
    request_id: str | None
    params: dict[str, Any]
    state: RunState
    current_attempt: int
    result: dict[str, Any] | None
    error: str | None
    state_version: int
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None


@dataclass(frozen=True)
class Attempt:
    run_id: UUID
    attempt: int
    fencing_token: int
    idempotency_token: str
    external_run_id: int | None
    state: AttemptState
    last_status: dict[str, Any] | None
    first_submitted_at: datetime | None
    terminal_at: datetime | None
    quarantine_until: datetime | None


_RUN_COLUMNS = (
    "run_id, job_key, lease_resource, requested_by, request_id, params, state, current_attempt, result, error, "
    "state_version, created_at, updated_at, terminal_at"
)
_ATTEMPT_COLUMNS = (
    "run_id, attempt, fencing_token, idempotency_token, external_run_id, state, last_status, first_submitted_at, "
    "terminal_at, quarantine_until"
)


def _run(row: Any) -> Run:
    return Run(
        run_id=row.run_id,
        job_key=row.job_key,
        lease_resource=row.lease_resource,
        requested_by=row.requested_by,
        request_id=row.request_id,
        params=dict(row.params),
        state=RunState(row.state),
        current_attempt=int(row.current_attempt),
        result=row.result,
        error=row.error,
        state_version=int(row.state_version),
        created_at=row.created_at,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
    )


def _attempt(row: Any) -> Attempt:
    return Attempt(
        run_id=row.run_id,
        attempt=int(row.attempt),
        fencing_token=int(row.fencing_token),
        idempotency_token=row.idempotency_token,
        external_run_id=None if row.external_run_id is None else int(row.external_run_id),
        state=AttemptState(row.state),
        last_status=row.last_status,
        first_submitted_at=row.first_submitted_at,
        terminal_at=row.terminal_at,
        quarantine_until=row.quarantine_until,
    )


class RunsRepository:
    def create_run(
        self,
        tx: Connection,
        *,
        run_id: UUID,
        job_key: str,
        lease_resource: str,
        requested_by: str,
        request_id: str | None,
        params: Mapping[str, Any],
    ) -> Run:
        row = tx.execute(
            text(
                f"INSERT INTO runs (run_id, job_key, lease_resource, requested_by, request_id, params, state) "
                f"VALUES (:id, :job, :res, :by, :req, CAST(:params AS jsonb), 'requested') RETURNING {_RUN_COLUMNS}"
            ),
            {
                "id": run_id,
                "job": job_key,
                "res": lease_resource,
                "by": requested_by,
                "req": request_id,
                "params": json.dumps(dict(params), sort_keys=True),
            },
        ).one()
        return _run(row)

    def create_attempt(
        self, tx: Connection, *, run_id: UUID, attempt: int, fencing_token: int, idempotency_token: str
    ) -> Attempt:
        row = tx.execute(
            text(
                f"INSERT INTO run_attempts (run_id, attempt, fencing_token, idempotency_token, state) "
                f"VALUES (:id, :a, :t, :tok, 'pending') RETURNING {_ATTEMPT_COLUMNS}"
            ),
            {"id": run_id, "a": attempt, "t": fencing_token, "tok": idempotency_token},
        ).one()
        tx.execute(text("UPDATE runs SET current_attempt = :a WHERE run_id = :id"), {"a": attempt, "id": run_id})
        return _attempt(row)

    def get_run(self, conn: Connection, run_id: UUID) -> Run | None:
        row = conn.execute(text(f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = :id"), {"id": run_id}).first()
        return None if row is None else _run(row)

    def get_attempt(self, conn: Connection, run_id: UUID, attempt: int) -> Attempt | None:
        row = conn.execute(
            text(f"SELECT {_ATTEMPT_COLUMNS} FROM run_attempts WHERE run_id = :id AND attempt = :a"),
            {"id": run_id, "a": attempt},
        ).first()
        return None if row is None else _attempt(row)

    def transition(
        self,
        tx: Connection,
        run_id: UUID,
        *,
        expected: Iterable[RunState],
        to: RunState,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Compare-and-set the run state. Returns False if the run was not in an expected state."""
        row = tx.execute(
            text(
                """
                UPDATE runs SET state = :to, state_version = state_version + 1, updated_at = now(),
                       terminal_at = CASE WHEN :terminal THEN now() ELSE terminal_at END,
                       result = COALESCE(CAST(:result AS jsonb), result), error = COALESCE(:error, error)
                 WHERE run_id = :id AND state = ANY(:expected)
                RETURNING run_id
                """
            ),
            {
                "to": to.value,
                "terminal": to in TERMINAL_RUN_STATES,
                "result": None if result is None else json.dumps(dict(result), sort_keys=True),
                "error": error,
                "id": run_id,
                "expected": [s.value for s in expected],
            },
        ).first()
        return row is not None

    def mark_submitted(self, tx: Connection, run_id: UUID, attempt: int, external_run_id: int) -> bool:
        row = tx.execute(
            text(
                "UPDATE run_attempts SET external_run_id = :ext, state = 'submitted', "
                "first_submitted_at = COALESCE(first_submitted_at, now()) "
                "WHERE run_id = :id AND attempt = :a AND state = 'pending' AND external_run_id IS NULL "
                "RETURNING attempt"
            ),
            {"ext": external_run_id, "id": run_id, "a": attempt},
        ).first()
        return row is not None

    def mark_running(self, tx: Connection, run_id: UUID, attempt: int, status: Mapping[str, Any]) -> bool:
        row = tx.execute(
            text(
                "UPDATE run_attempts SET state = 'running', last_status = CAST(:s AS jsonb) "
                "WHERE run_id = :id AND attempt = :a AND state = 'submitted' RETURNING attempt"
            ),
            {"s": json.dumps(dict(status), sort_keys=True), "id": run_id, "a": attempt},
        ).first()
        return row is not None

    def mark_terminated(
        self,
        tx: Connection,
        run_id: UUID,
        attempt: int,
        status: Mapping[str, Any],
        *,
        quarantine: timedelta | None = None,
    ) -> bool:
        row = tx.execute(
            text(
                """
                UPDATE run_attempts SET state = 'terminated', terminal_at = now(), last_status = CAST(:s AS jsonb),
                       quarantine_until = CASE WHEN CAST(:q_s AS integer) IS NULL THEN NULL
                                               ELSE now() + make_interval(secs => CAST(:q_s AS integer)) END
                 WHERE run_id = :id AND attempt = :a AND state IN ('submitted', 'running')
                RETURNING attempt
                """
            ),
            {
                "s": json.dumps(dict(status), sort_keys=True),
                "q_s": None if quarantine is None else int(quarantine.total_seconds()),
                "id": run_id,
                "a": attempt,
            },
        ).first()
        return row is not None

    def mark_submit_failed(self, tx: Connection, run_id: UUID, attempt: int, reason: str) -> bool:
        row = tx.execute(
            text(
                "UPDATE run_attempts SET state = 'submit_failed', terminal_at = now(), "
                "last_status = jsonb_build_object('reason', CAST(:reason AS text)) "
                "WHERE run_id = :id AND attempt = :a AND state = 'pending' RETURNING attempt"
            ),
            {"reason": reason, "id": run_id, "a": attempt},
        ).first()
        return row is not None

    def attempts_to_observe(self, conn: Connection, limit: int = 100) -> list[Attempt]:
        rows = conn.execute(
            text(
                f"SELECT {_ATTEMPT_COLUMNS} FROM run_attempts WHERE state IN ('submitted', 'running') "
                "AND external_run_id IS NOT NULL ORDER BY first_submitted_at NULLS FIRST LIMIT :n"
            ),
            {"n": limit},
        ).all()
        return [_attempt(r) for r in rows]

    def quarantine_elapsed(self, conn: Connection) -> list[Attempt]:
        """Terminated attempts whose quarantine is over but which still hold their lease."""
        rows = conn.execute(
            text(
                f"SELECT {', '.join('a.' + c.strip() for c in _ATTEMPT_COLUMNS.split(','))} "
                "FROM run_attempts a JOIN runs r ON r.run_id = a.run_id "
                "JOIN leases l ON l.resource = r.lease_resource "
                "AND l.holder_run_id = a.run_id AND l.holder_attempt = a.attempt "
                "WHERE a.state = 'terminated' AND a.quarantine_until IS NOT NULL AND a.quarantine_until <= now()"
            )
        ).all()
        return [_attempt(r) for r in rows]
