"""RunCoordinator: starts, submits, watches and finishes engine runs without Temporal (ADR-0006).

Flow for every run:

1. ``start`` (one transaction, called by a request handler): insert the run, acquire the writer lease
   for its resource (fencing token), insert attempt 1, publish ``run.submit``. No external calls.
2. ``handle_submit`` (outbox relay, at least once): submit to the Jobs API with the attempt's
   idempotency token, then compare-and-set the attempt to ``submitted``. Redelivery inside the token
   window returns the same external run. After the window it searches ``runs/list`` by the run_id
   and attempt parameters instead of submitting again.
3. ``poll`` (scheduler): observe active attempts. UNKNOWN changes nothing. A terminal status is
   recorded in one transaction together with the run outcome and, unless the attempt is quarantined
   after an INTERNAL_ERROR or CLOUD_FAILURE, the lease release.
4. ``request_cancel`` publishes ``run.cancel``, and ``handle_cancel`` asks the Jobs API to cancel.
   The run finishes through ``poll`` like any other.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine

from close_contracts.enums import Env, JobStatus
from close_platform.compute.base import ComputeBackend, JobSpec, RunObservation
from close_platform.db.engine import db_now
from close_platform.leases import Grant, LeaseManager
from close_platform.outbox import Outbox, OutboxEvent
from close_platform.runs import ACTIVE_RUN_STATES, Attempt, AttemptState, Run, RunsRepository, RunState

log = logging.getLogger(__name__)

TOPIC_SUBMIT = "run.submit"
TOPIC_CANCEL = "run.cancel"
# Never dead-lettered: submit ends its own loop (find() or submit_failed once the token window has
# passed) and cancel becomes a no-op once the attempt is terminal. Giving up earlier would leave the
# writer lease held by an attempt that nothing observes any more.
DURABLE_TOPICS = frozenset({TOPIC_SUBMIT, TOPIC_CANCEL})
QUARANTINE_TYPES = frozenset({"INTERNAL_ERROR", "CLOUD_FAILURE"})

ParamBuilder = Callable[[Run, Attempt], Mapping[str, str]]
"""Builds the Jobs API job_parameters for an attempt (the job parameter contract)."""


@dataclass(frozen=True)
class StartedRun:
    run: Run
    grant: Grant
    attempt: Attempt


class RunCoordinator:
    def __init__(
        self,
        engine: Engine,
        compute: ComputeBackend,
        *,
        env: Env,
        actor: str,
        job_ids: Mapping[str, int],
        build_params: ParamBuilder,
        quarantine: timedelta = timedelta(minutes=10),
        token_window: timedelta = timedelta(hours=1),
    ) -> None:
        self._engine = engine
        self._compute = compute
        self._env = env
        self._job_ids = dict(job_ids)
        self._build_params = build_params
        self._quarantine = quarantine
        self._token_window = token_window
        self.runs = RunsRepository()
        self.leases = LeaseManager(actor)
        self.outbox = Outbox()

    # ------------------------------------------------------------------ requests (inside a transaction)

    def start(
        self,
        tx: Connection,
        *,
        job_key: str,
        lease_resource: str,
        requested_by: str,
        request_id: str | None,
        params: Mapping[str, Any],
        run_id: UUID | None = None,
    ) -> StartedRun:
        """Create the run, take the lease (LeaseBusyError if held) and queue the submission."""
        if job_key not in self._job_ids:
            raise KeyError(f"unknown job {job_key!r}")
        run_id = run_id or uuid.uuid4()
        run = self.runs.create_run(
            tx,
            run_id=run_id,
            job_key=job_key,
            lease_resource=lease_resource,
            requested_by=requested_by,
            request_id=request_id,
            params=params,
        )
        grant = self.leases.acquire(tx, lease_resource, run_id, attempt=1)
        spec_token = JobSpec(job_key, self._job_ids[job_key], run_id, 1).idempotency_token(self._env)
        attempt = self.runs.create_attempt(
            tx, run_id=run_id, attempt=1, fencing_token=grant.fencing_token, idempotency_token=spec_token
        )
        self.outbox.publish(
            tx, OutboxEvent(uuid.uuid4(), TOPIC_SUBMIT, f"run:{run_id}", {"run_id": str(run_id), "attempt": 1})
        )
        return StartedRun(run, grant, attempt)

    def request_cancel(self, tx: Connection, run_id: UUID) -> bool:
        run = self.runs.get_run(tx, run_id)
        if run is None or run.state not in ACTIVE_RUN_STATES:
            return False
        if not self.runs.transition(
            tx, run_id, expected=ACTIVE_RUN_STATES - {RunState.CANCEL_REQUESTED}, to=RunState.CANCEL_REQUESTED
        ):
            return run.state == RunState.CANCEL_REQUESTED
        self.outbox.publish(
            tx,
            OutboxEvent(
                uuid.uuid4(), TOPIC_CANCEL, f"run:{run_id}", {"run_id": str(run_id), "attempt": run.current_attempt}
            ),
        )
        return True

    # ------------------------------------------------------------------ outbox handlers (outside transactions)

    def _spec(self, run: Run, attempt: Attempt) -> JobSpec:
        return JobSpec(
            run.job_key, self._job_ids[run.job_key], run.run_id, attempt.attempt, self._build_params(run, attempt)
        )

    def handle_submit(self, event: OutboxEvent) -> None:
        run_id, n = UUID(str(event.payload["run_id"])), int(str(event.payload["attempt"]))
        with self._engine.connect() as conn:
            run = self.runs.get_run(conn, run_id)
            attempt = self.runs.get_attempt(conn, run_id, n)
            now = db_now(conn)
        if run is None or attempt is None:
            raise LookupError(f"run {run_id} attempt {n} not found")
        if attempt.external_run_id is not None or attempt.state != "pending":
            return  # already submitted (redelivery)
        spec = self._spec(run, attempt)
        since = run.created_at - timedelta(minutes=5)
        if now - run.created_at > self._token_window:
            found = self._compute.find(spec, since=since)
            if found is None:
                self._submit_failed(run, attempt, "token window passed and no run was found")
                return
            external = found
        elif event.delivery > 1:
            # An earlier delivery may have reached run-now before it failed or crashed. Look first, so a
            # duplicate would need both a missed lookup and a forgotten idempotency token.
            external = self._compute.find(spec, since=since) or self._compute.submit(spec, self._env)
        else:
            external = self._compute.submit(spec, self._env)
        with self._engine.begin() as tx:
            if self.runs.mark_submitted(tx, run_id, n, external):
                self.runs.transition(tx, run_id, expected={RunState.REQUESTED}, to=RunState.SUBMITTED)

    def _submit_failed(self, run: Run, attempt: Attempt, reason: str) -> None:
        with self._engine.begin() as tx:
            if self.runs.mark_submit_failed(tx, run.run_id, attempt.attempt, reason):
                self.runs.transition(
                    tx, run.run_id, expected=ACTIVE_RUN_STATES, to=RunState.SUBMIT_FAILED, error=reason
                )
                holder = self.leases.holder(tx, run.lease_resource)
                if holder is not None and holder.run_id == run.run_id and holder.attempt == attempt.attempt:
                    self.leases.release(tx, holder, reason="submit_failed")

    def handle_cancel(self, event: OutboxEvent) -> None:
        run_id, n = UUID(str(event.payload["run_id"])), int(str(event.payload["attempt"]))
        with self._engine.connect() as conn:
            attempt = self.runs.get_attempt(conn, run_id, n)
        if attempt is None:
            raise LookupError(f"run {run_id} attempt {n} not found")
        if attempt.state in {AttemptState.TERMINATED, AttemptState.SUBMIT_FAILED}:
            return  # nothing left to cancel
        if attempt.external_run_id is None:
            raise RuntimeError("cancel requested before submission; retry after the run is submitted")
        self._compute.cancel(attempt.external_run_id)

    def handlers(self) -> dict[str, Callable[[OutboxEvent], None]]:
        return {TOPIC_SUBMIT: self.handle_submit, TOPIC_CANCEL: self.handle_cancel}

    # ------------------------------------------------------------------ scheduler tasks

    def poll(self) -> int:
        """Observe active attempts and record progress. Returns the number of attempts that finished."""
        with self._engine.connect() as conn:
            attempts = self.runs.attempts_to_observe(conn)
        finished = 0
        for attempt in attempts:
            if attempt.external_run_id is None:  # attempts_to_observe only returns submitted attempts
                continue
            obs = self._compute.observe(attempt.external_run_id)
            if obs.status == JobStatus.UNKNOWN:
                continue  # nothing is known: no transition, the lease stays held
            if not obs.status.is_terminal:
                if obs.status == JobStatus.RUNNING:
                    with self._engine.begin() as tx:
                        if self.runs.mark_running(tx, attempt.run_id, attempt.attempt, obs.as_dict()):
                            self.runs.transition(tx, attempt.run_id, expected={RunState.SUBMITTED}, to=RunState.RUNNING)
                continue
            if self._finish(attempt, obs):
                finished += 1
        self.release_quarantined()
        return finished

    def _finish(self, attempt: Attempt, obs: RunObservation) -> bool:
        result: Mapping[str, object] | None = None
        error: str | None = None
        if obs.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:  # a failed job may say why (fenced)
            try:
                result = self._compute.result(obs)
            except Exception as exc:  # output unavailable: a success without its result is a failure
                if obs.status == JobStatus.SUCCEEDED:
                    error = f"result unavailable: {type(exc).__name__}: {exc}"[:2000]
        outcome = {
            JobStatus.SUCCEEDED: RunState.SUCCEEDED,
            JobStatus.CANCELLED: RunState.CANCELLED,
        }.get(obs.status, RunState.FAILED)
        if result is not None and result.get("fenced"):
            outcome = RunState.FAILED
            error = (
                f"fenced: a newer writer holds the lease (stage {result.get('fenced_stage')}, "
                f"own token {attempt.fencing_token}, highest {result.get('highest_token')})"
            )
        elif outcome == RunState.SUCCEEDED and result is None:
            outcome = RunState.FAILED
            error = error or "job produced no result line"
        if outcome == RunState.FAILED and error is None:
            error = obs.message or obs.termination_code or "job failed"
        quarantine = self._quarantine if obs.termination_type in QUARANTINE_TYPES else None
        with self._engine.begin() as tx:
            if not self.runs.mark_terminated(tx, attempt.run_id, attempt.attempt, obs.as_dict(), quarantine=quarantine):
                return False  # another instance recorded it first
            self.runs.transition(tx, attempt.run_id, expected=ACTIVE_RUN_STATES, to=outcome, result=result, error=error)
            run = self.runs.get_run(tx, attempt.run_id)
            if run is None:  # the attempt row references its run, so this cannot happen
                raise LookupError(f"run {attempt.run_id} vanished")
            holder = self.leases.holder(tx, run.lease_resource)
            if (
                quarantine is None
                and holder is not None
                and (holder.run_id, holder.attempt)
                == (
                    attempt.run_id,
                    attempt.attempt,
                )
            ):
                self.leases.release(tx, holder, reason=f"attempt {outcome.value}")
        return True

    def release_quarantined(self) -> int:
        with self._engine.connect() as conn:
            due = self.runs.quarantine_elapsed(conn)
        released = 0
        for attempt in due:
            with self._engine.begin() as tx:
                run = self.runs.get_run(tx, attempt.run_id)
                if run is None:
                    continue
                holder = self.leases.holder(tx, run.lease_resource)
                if holder is not None and (holder.run_id, holder.attempt) == (attempt.run_id, attempt.attempt):
                    self.leases.release(tx, holder, reason="quarantine elapsed")
                    released += 1
        return released


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
