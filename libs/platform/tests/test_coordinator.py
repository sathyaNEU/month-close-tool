"""RunCoordinator against real Postgres with the fake compute backend (lifecycle, faults, invariants)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from close_contracts.enums import Env, JobStatus
from close_platform.compute.fake import FakeComputeBackend
from close_platform.coordinator import DURABLE_TOPICS, RunCoordinator
from close_platform.leases import LeaseBusyError
from close_platform.outbox import OutboxEvent, Relay
from close_platform.runs import Attempt, Run, RunState

pytestmark = pytest.mark.pg


def _params(run: Run, attempt: Attempt) -> Mapping[str, str]:
    return {
        "run_id": run.run_id.hex,
        "attempt": str(attempt.attempt),
        "lease_resource": run.lease_resource,
        "fencing_token": str(attempt.fencing_token),
    }


def _coordinator(engine: Engine, fake: FakeComputeBackend, **kw: object) -> RunCoordinator:
    return RunCoordinator(
        engine,
        fake,
        env=Env.LOCAL,
        actor="app1",
        job_ids={"smoke": 1001},
        build_params=_params,
        **kw,  # type: ignore[arg-type]
    )


def _start(engine: Engine, coord: RunCoordinator, resource: str = "smoke:default") -> uuid.UUID:
    with engine.begin() as tx:
        started = coord.start(
            tx, job_key="smoke", lease_resource=resource, requested_by="u1", request_id="r1", params={"rows": 10}
        )
    return started.run.run_id


def _run(engine: Engine, coord: RunCoordinator, run_id: uuid.UUID) -> Run:
    with engine.connect() as conn:
        run = coord.runs.get_run(conn, run_id)
    assert run is not None
    return run


def _holder(engine: Engine, coord: RunCoordinator, resource: str = "smoke:default") -> object:
    with engine.connect() as conn:
        return coord.leases.holder(conn, resource)


def test_happy_path_submits_once_and_releases_on_success(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake)
    relay = Relay(engine, coord.handlers(), instance_id="app1")
    run_id = _start(engine, coord)
    assert relay.run_once() == 1
    assert _run(engine, coord, run_id).state == RunState.SUBMITTED
    ext = next(iter(fake.runs))
    assert fake.runs[ext].spec.params["fencing_token"] == "1"
    fake.advance(ext, JobStatus.RUNNING)
    coord.poll()
    assert _run(engine, coord, run_id).state == RunState.RUNNING
    fake.advance(ext, JobStatus.SUCCEEDED, result={"rows": 10, "fenced": False})
    assert coord.poll() == 1
    run = _run(engine, coord, run_id)
    assert (run.state, run.result) == (RunState.SUCCEEDED, {"rows": 10, "fenced": False})
    assert _holder(engine, coord) is None


def test_second_start_on_a_held_resource_is_refused_and_leaves_nothing_behind(engine: Engine) -> None:
    coord = _coordinator(engine, FakeComputeBackend())
    _start(engine, coord)
    with pytest.raises(LeaseBusyError):
        _start(engine, coord)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 1  # the refused run rolled back


def test_redelivered_submit_starts_exactly_one_external_run(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake)
    run_id = _start(engine, coord)
    event = {"run_id": str(run_id), "attempt": 1}
    from close_platform.outbox import OutboxEvent

    for _ in range(20):
        coord.handle_submit(OutboxEvent(uuid.uuid4(), "run.submit", f"run:{run_id}", event))
    assert len(fake.runs) == 1


def test_unknown_status_freezes_everything(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake)
    run_id = _start(engine, coord)
    Relay(engine, coord.handlers(), instance_id="app1").run_once()
    fake.unavailable = True
    for _ in range(5):
        assert coord.poll() == 0
    assert _run(engine, coord, run_id).state == RunState.SUBMITTED
    assert _holder(engine, coord) is not None
    with pytest.raises(LeaseBusyError):
        _start(engine, coord)


def test_internal_error_quarantines_the_lease(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake, quarantine=timedelta(seconds=1))
    run_id = _start(engine, coord)
    Relay(engine, coord.handlers(), instance_id="app1").run_once()
    ext = next(iter(fake.runs))
    fake.advance(ext, JobStatus.FAILED, termination_type="INTERNAL_ERROR", termination_code="DRIVER_ERROR")
    coord.poll()
    assert _run(engine, coord, run_id).state == RunState.FAILED
    assert _holder(engine, coord) is not None  # still held during quarantine
    with engine.begin() as tx:
        tx.execute(text("UPDATE run_attempts SET quarantine_until = now() - interval '1 second'"))
    assert coord.release_quarantined() == 1
    assert _holder(engine, coord) is None


def test_fenced_result_fails_the_run(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake)
    run_id = _start(engine, coord)
    Relay(engine, coord.handlers(), instance_id="app1").run_once()
    ext = next(iter(fake.runs))
    fake.advance(ext, JobStatus.SUCCEEDED, result={"fenced": True})
    coord.poll()
    run = _run(engine, coord, run_id)
    assert run.state == RunState.FAILED
    assert "fenced" in (run.error or "")


def test_cancel_flows_through_the_outbox_and_releases(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake)
    relay = Relay(engine, coord.handlers(), instance_id="app1")
    run_id = _start(engine, coord)
    relay.run_once()
    with engine.begin() as tx:
        assert coord.request_cancel(tx, run_id)
    relay.run_once()
    assert fake.cancel_calls == 1
    coord.poll()
    assert _run(engine, coord, run_id).state == RunState.CANCELLED
    assert _holder(engine, coord) is None


def test_after_the_token_window_submit_searches_instead_of_resubmitting(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake, token_window=timedelta(seconds=0))
    run_id = _start(engine, coord)
    from close_platform.outbox import OutboxEvent

    event = OutboxEvent(uuid.uuid4(), "run.submit", f"run:{run_id}", {"run_id": str(run_id), "attempt": 1})
    coord.handle_submit(event)  # nothing found after the window -> submit_failed, lease released
    run = _run(engine, coord, run_id)
    assert run.state == RunState.SUBMIT_FAILED
    assert fake.submit_calls == 0
    assert _holder(engine, coord) is None


def test_submit_failure_is_retried_by_the_relay_without_duplicates(engine: Engine) -> None:
    fake = FakeComputeBackend(fail_submit=True)
    coord = _coordinator(engine, fake)
    relay = Relay(engine, coord.handlers(), instance_id="app1")
    run_id = _start(engine, coord)
    assert relay.run_once() == 0
    fake.fail_submit = False
    with engine.begin() as tx:
        tx.execute(text("UPDATE outbox SET available_at = now()"))
    assert relay.run_once() == 1
    assert len(fake.runs) == 1
    assert _run(engine, coord, run_id).state == RunState.SUBMITTED


def _due_now(engine: Engine) -> None:
    with engine.begin() as tx:  # skip the backoff wait
        tx.execute(text("UPDATE outbox SET available_at = now() WHERE delivered_at IS NULL"))


def test_a_long_outage_never_dead_letters_the_submit_or_strands_the_lease(engine: Engine) -> None:
    fake = FakeComputeBackend(unavailable=True)
    coord = _coordinator(engine, fake)
    relay = Relay(engine, coord.handlers(), instance_id="app1", max_attempts=3, never_dead_letter=DURABLE_TOPICS)
    run_id = _start(engine, coord)
    for _ in range(6):  # twice the dead-letter threshold
        _due_now(engine)
        assert relay.run_once() == 0
    with engine.connect() as conn:
        row = conn.execute(text("SELECT attempts, dead_at FROM outbox")).one()
    assert row.attempts == 6
    assert row.dead_at is None
    fake.unavailable = False
    _due_now(engine)
    assert relay.run_once() == 1
    assert _run(engine, coord, run_id).state == RunState.SUBMITTED
    assert len(fake.runs) == 1


def test_other_topics_are_still_dead_lettered(engine: Engine) -> None:
    relay = Relay(engine, {}, instance_id="app1", max_attempts=2, never_dead_letter=DURABLE_TOPICS)
    from close_platform.outbox import Outbox

    with engine.begin() as tx:
        Outbox().publish(tx, OutboxEvent(uuid.uuid4(), "webhook.send", "hook:1", {}))
    for _ in range(2):
        _due_now(engine)
        relay.run_once()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT dead_at IS NOT NULL FROM outbox")).scalar_one()


def test_a_redelivery_looks_for_the_run_before_submitting_again(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake)
    run_id = _start(engine, coord)
    real_submit = fake.submit

    def lost_response(spec: object, env: Env) -> int:
        real_submit(spec, env)  # type: ignore[arg-type]
        raise ConnectionError("response lost after the run was created")

    fake.submit = lost_response  # type: ignore[method-assign]
    relay = Relay(engine, coord.handlers(), instance_id="app1", never_dead_letter=DURABLE_TOPICS)
    assert relay.run_once() == 0
    fake.submit = real_submit  # type: ignore[method-assign]
    fake.by_token.clear()  # Databricks forgot the idempotency token (its retention is not documented)
    _due_now(engine)
    assert relay.run_once() == 1
    assert len(fake.runs) == 1  # found, not submitted twice
    assert _run(engine, coord, run_id).state == RunState.SUBMITTED


def test_cancel_after_submit_failed_is_a_no_op(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake, token_window=timedelta(seconds=0))
    run_id = _start(engine, coord)
    with engine.begin() as tx:
        coord.request_cancel(tx, run_id)
    relay = Relay(engine, coord.handlers(), instance_id="app1", never_dead_letter=DURABLE_TOPICS)
    assert relay.run_once() == 1  # submit ends submit_failed (FIFO: the cancel waits for it)
    assert relay.run_once() == 1  # the cancel has nothing left to do
    assert fake.cancel_calls == 0
    assert _run(engine, coord, run_id).state == RunState.SUBMIT_FAILED
    assert _holder(engine, coord) is None


def test_a_failed_job_that_was_fenced_says_so(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake)
    relay = Relay(engine, coord.handlers(), instance_id="app1", never_dead_letter=DURABLE_TOPICS)
    run_id = _start(engine, coord)
    relay.run_once()
    (ext,) = fake.runs
    fenced = {"fenced": True, "fenced_stage": "before_commit", "highest_token": 9, "rows": 0}
    fake.advance(ext, JobStatus.FAILED, result=fenced)  # the engine exits 3 when fenced
    coord.poll()
    run = _run(engine, coord, run_id)
    assert run.state == RunState.FAILED
    assert run.error == "fenced: a newer writer holds the lease (stage before_commit, own token 1, highest 9)"
    assert _holder(engine, coord) is None


def test_a_failed_job_without_output_keeps_the_termination_message(engine: Engine) -> None:
    fake = FakeComputeBackend()
    coord = _coordinator(engine, fake)
    relay = Relay(engine, coord.handlers(), instance_id="app1", never_dead_letter=DURABLE_TOPICS)
    run_id = _start(engine, coord)
    relay.run_once()
    (ext,) = fake.runs
    fake.advance(ext, JobStatus.FAILED)
    coord.poll()
    assert _run(engine, coord, run_id).error == "RUN_EXECUTION_ERROR"
