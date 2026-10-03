"""Smoke runs through the API: idempotent start, one writer, entity-filtered results, cancel (Gate 1a)."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from apptestkit import run_to, who
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from close_app.context import AppContext
from close_contracts.enums import JobStatus
from close_platform.compute.fake import FakeComputeBackend
from close_platform.outbox import Relay
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg


def _with_key(user: str, key: str) -> dict[str, str]:
    return who(user, **{"Idempotency-Key": key})


def _start(client: TestClient, user: str = "alice", key: str | None = None, rows: int = 1000) -> Any:
    return client.post("/api/v1/smoke/runs", json={"rows": rows}, headers=_with_key(user, key or f"key-{uuid.uuid4()}"))


def _count(engine: Engine, sql: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())


def test_start_needs_an_idempotency_key(client: TestClient) -> None:
    r = client.post("/api/v1/smoke/runs", json={"rows": 10}, headers=who("alice"))
    assert r.status_code == 400
    assert r.json()["title"] == "Idempotency-Key header is required"
    short = client.post("/api/v1/smoke/runs", json={"rows": 10}, headers=_with_key("alice", "short"))
    assert short.status_code == 400
    twice = [*who("alice").items(), ("Idempotency-Key", "key-aaaaaaaa"), ("Idempotency-Key", "key-bbbbbbbb")]
    assert client.post("/api/v1/smoke/runs", json={"rows": 10}, headers=twice).status_code == 400


@pytest.mark.parametrize("body", [{"rows": 0}, {"rows": 10_000_001}, {"rows": 5, "extra": 1}, {"rows": "many"}])
def test_invalid_bodies_are_422_problems(client: TestClient, body: dict[str, Any]) -> None:
    r = client.post("/api/v1/smoke/runs", json=body, headers=_with_key("alice", "invalid-body-01"))
    assert r.status_code == 422
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["errors"]


def test_start_commits_run_lease_and_outbox_event_together(client: TestClient, engine: Engine) -> None:
    r = _start(client)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["state"] == "requested"
    assert body["attempt"] == 1
    assert body["fencing_token"] == 1
    assert _count(engine, "SELECT count(*) FROM runs") == 1
    assert _count(engine, "SELECT count(*) FROM outbox WHERE topic = 'run.submit'") == 1
    with engine.connect() as conn:
        lease = conn.execute(text("SELECT holder_run_id, fencing_token FROM leases")).one()
    assert str(lease.holder_run_id) == body["run_id"]
    assert lease.fencing_token == 1


def test_replay_returns_the_stored_response_without_a_second_run(client: TestClient, engine: Engine) -> None:
    first = _start(client, key="order-key-1")
    again = _start(client, key="order-key-1")
    assert again.status_code == 202
    assert again.json() == first.json()
    assert again.headers["idempotent-replayed"] == "true"
    assert _count(engine, "SELECT count(*) FROM runs") == 1
    reused = _start(client, key="order-key-1", rows=5)
    assert reused.status_code == 422
    assert reused.json()["title"] == "Idempotency-Key reused with a different request"


def test_keys_are_scoped_per_principal(client: TestClient) -> None:
    assert _start(client, "alice", key="shared-key-1").status_code == 202
    bob = _start(client, "bob", key="shared-key-1")
    assert bob.status_code == 409  # a new request for bob (the lease is busy), not a replay of alice's
    assert "idempotent-replayed" not in bob.headers


def test_a_failed_request_stores_nothing_so_a_retry_executes(client: TestClient, engine: Engine) -> None:
    assert _start(client, "alice").status_code == 202
    assert _start(client, "bob", key="retry-key-1").status_code == 409
    assert _count(engine, "SELECT count(*) FROM idempotency_keys WHERE key = 'retry-key-1'") == 0


def test_twenty_parallel_starts_give_one_202_and_nineteen_409(client: TestClient, engine: Engine) -> None:
    barrier = threading.Barrier(20)

    def go(i: int) -> Any:
        barrier.wait()
        return _start(client, ["alice", "bob", "dave"][i % 3], key=f"parallel-{i:04d}")

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(go, range(20)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [202] + [409] * 19, codes
    winner = next(r.json() for r in responses if r.status_code == 202)
    busy = [r.json() for r in responses if r.status_code == 409]
    assert {b["holder_run_id"] for b in busy} == {winner["run_id"]}
    assert _count(engine, "SELECT count(*) FROM runs") == 1
    assert _count(engine, "SELECT fencing_token FROM leases") == 1  # rose by exactly one
    assert _count(engine, "SELECT count(*) FROM lease_events WHERE event = 'granted'") == 1


def test_parallel_requests_with_one_key_start_one_run_and_replay_the_rest(client: TestClient, engine: Engine) -> None:
    barrier = threading.Barrier(10)

    def go(_: int) -> Any:
        barrier.wait()
        return _start(client, "alice", key="same-key-parallel")

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(go, range(10)))
    assert [r.status_code for r in responses] == [202] * 10
    assert len({r.text for r in responses}) == 1  # identical bodies
    assert sum(r.headers.get("idempotent-replayed") == "true" for r in responses) == 9
    assert _count(engine, "SELECT count(*) FROM runs") == 1


def test_results_are_filtered_to_the_callers_entities(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> None:
    run_id = run_to(client, relay, ctx, fake, JobStatus.SUCCEEDED, lake=lake)
    alice = client.get(f"/api/v1/smoke/runs/{run_id}", headers=who("alice")).json()
    assert alice["state"] == "succeeded"
    assert alice["result"]["entities"] == ["US01"]
    assert alice["result"]["rows"] == 600
    assert alice["result"]["digests_by_entity"] == {"US01": "d-us"}
    bob = client.get(f"/api/v1/smoke/runs/{run_id}", headers=who("bob")).json()["result"]
    assert (bob["rows"], bob["digests_by_entity"]) == (400, {"EU01": "d-eu"})
    carol = client.get(f"/api/v1/smoke/runs/{run_id}", headers=who("carol")).json()["result"]
    assert (carol["entities"], carol["rows"], carol["digests_by_entity"]) == ([], 0, {})
    dave = client.get(f"/api/v1/smoke/runs/{run_id}", headers=who("dave")).json()["result"]
    assert dave["rows"] == 1000
    assert "digest" not in dave  # the all-entity digest is never exposed


def test_run_parameters_follow_the_job_contract(
    client: TestClient, relay: Relay, fake: FakeComputeBackend, engine: Engine
) -> None:
    run_id = _start(client, rows=77, key="contract-key-1").json()["run_id"]
    relay.run_once()
    (submitted,) = fake.runs.values()
    params = dict(submitted.spec.params)
    with engine.connect() as conn:
        created = conn.execute(text("SELECT created_at FROM runs")).scalar_one()
    assert params == {
        "run_id": uuid.UUID(run_id).hex,
        "attempt": "1",
        "lease_resource": "smoke:default",
        "fencing_token": "1",
        "requested_at": created.isoformat(),
        "request_id": "none",
        "root_uri": "s3a://test/lake",
        "rows": "77",
    }
    assert submitted.token == f"close-local-smoke-{uuid.UUID(run_id).hex}-a01"


def test_the_next_run_gets_the_next_fencing_token(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> None:
    run_to(client, relay, ctx, fake, JobStatus.SUCCEEDED, lake=lake)
    nxt = _start(client, "bob")
    assert nxt.status_code == 202
    assert nxt.json()["fencing_token"] == 2


def test_a_failed_run_keeps_its_error_and_frees_the_lease(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend
) -> None:
    run_id = run_to(client, relay, ctx, fake, JobStatus.FAILED)
    run = client.get(f"/api/v1/smoke/runs/{run_id}", headers=who("alice")).json()
    assert run["state"] == "failed"
    assert run["error"]
    assert run["result"] is None
    assert _start(client).status_code == 202


def test_unknown_status_keeps_the_lease_and_blocks_new_starts(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend
) -> None:
    run_id = _start(client).json()["run_id"]
    relay.run_once()
    fake.unavailable = True
    ctx.coordinator.poll()
    assert client.get(f"/api/v1/smoke/runs/{run_id}", headers=who("alice")).json()["state"] == "submitted"
    assert _start(client, "bob").status_code == 409


def test_cancel_flows_through_the_outbox(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend
) -> None:
    run_id = _start(client).json()["run_id"]
    relay.run_once()
    r = client.post(
        f"/api/v1/smoke/runs/{run_id}:cancel", headers=who("bob", **{"Idempotency-Key": f"cancel-{uuid.uuid4()}"})
    )
    assert r.status_code == 202
    assert r.json()["state"] == "cancel_requested"
    assert (
        client.post(
            f"/api/v1/smoke/runs/{run_id}:cancel", headers=who("bob", **{"Idempotency-Key": f"cancel-{uuid.uuid4()}"})
        ).status_code
        == 202
    )  # idempotent
    relay.run_once()
    assert fake.cancel_calls == 1
    ctx.coordinator.poll()
    assert client.get(f"/api/v1/smoke/runs/{run_id}", headers=who("alice")).json()["state"] == "cancelled"
    done = client.post(
        f"/api/v1/smoke/runs/{run_id}:cancel", headers=who("bob", **{"Idempotency-Key": f"cancel-{uuid.uuid4()}"})
    )
    assert done.status_code == 409
    assert _start(client).status_code == 202  # the lease was released


def test_unknown_and_malformed_run_ids(client: TestClient) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/smoke/runs/{missing}", headers=who("alice")).status_code == 404
    assert (
        client.post(
            f"/api/v1/smoke/runs/{missing}:cancel", headers=who("alice", **{"Idempotency-Key": "cancel-missing"})
        ).status_code
        == 404
    )
    assert client.get("/api/v1/smoke/runs/not-a-uuid", headers=who("alice")).status_code == 422
    assert client.get(f"/api/v1/smoke/runs/{missing}").status_code == 401
