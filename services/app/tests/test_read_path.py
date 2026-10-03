"""The read path through the API (Gate 1b): results come from the lake bound to the right writer, pages
come through signed cursors, and every query is scoped to the caller's entities."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from apptestkit import publish_manifest, run_to, start, token_of, who
from fastapi.testclient import TestClient
from querykit import dataset

from close_app.context import AppContext, build_context
from close_app.main import create_app
from close_app.settings import Settings
from close_contracts.enums import JobStatus
from close_contracts.query import Row
from close_platform.compute.fake import FakeComputeBackend
from close_platform.outbox import Relay
from close_platform.query import QueryBusyError, QueryTimeoutError
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg


def succeeded(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> str:
    return run_to(client, relay, ctx, fake, JobStatus.SUCCEEDED, lake=lake)


def load_rows(lake: ReferenceBackend, run_id: str, n: int = 150) -> list[Row]:
    rows = [{**r, "run_id": uuid.UUID(run_id).hex} for r in dataset(n)]
    lake.load("ops.smoke_results", rows)
    return rows


def get(client: TestClient, path: str, user: str, **params: Any) -> Any:
    return client.get(path, headers=who(user), params=params)


def test_only_the_manifest_of_the_winning_attempt_and_token_counts(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> None:
    run_id = succeeded(client, relay, ctx, fake, lake)
    token = token_of(ctx.engine, run_id)
    zombie: Row = {
        "run_id": uuid.UUID(run_id).hex, "attempt": 1, "fencing_token": token - 1, "job": "smoke",
        "entity_id": "US01", "row_count": 7, "digest": "zombie",
    }  # fmt: skip
    publish_manifest(lake, run_id, token, extra=[zombie])
    result = get(client, f"/api/v1/smoke/runs/{run_id}", "alice").json()["result"]
    assert result["digests_by_entity"] == {"US01": "d-us"}
    assert result["rows"] == 600


def test_identical_repeats_merge_and_conflicting_digests_are_an_integrity_error(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> None:
    run_id = succeeded(client, relay, ctx, fake, lake)
    token = token_of(ctx.engine, run_id)
    publish_manifest(lake, run_id, token, extra=[dict(lake.snapshots["ops.run_manifests"][-1][0])])
    assert get(client, f"/api/v1/smoke/runs/{run_id}", "dave").json()["result"]["rows"] == 1000
    conflicting = {**lake.snapshots["ops.run_manifests"][-1][0], "digest": "different"}
    publish_manifest(lake, run_id, token, extra=[conflicting])
    r = get(client, f"/api/v1/smoke/runs/{run_id}", "dave")
    assert r.status_code == 500
    assert r.json()["title"] == "Result integrity error"


def test_paging_rows_returns_the_callers_rows_exactly_once(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> None:
    run_id = succeeded(client, relay, ctx, fake, lake)
    rows = load_rows(lake, run_id)
    seen: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"page_size": 17} | ({"cursor": cursor} if cursor else {})
        body = get(client, f"/api/v1/smoke/runs/{run_id}/rows", "alice", **params).json()
        seen.extend(body["rows"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    expected = sorted(
        (r for r in rows if r["entity_id"] == "US01"), key=lambda r: (-int(str(r["amount_minor"])), r["row_id"])
    )
    assert [r["row_id"] for r in seen] == [r["row_id"] for r in expected]
    assert {r["entity_id"] for r in seen} == {"US01"}


def test_cursors_cannot_be_reused_by_another_user_or_with_other_filters(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> None:
    run_id = succeeded(client, relay, ctx, fake, lake)
    load_rows(lake, run_id)
    first = get(client, f"/api/v1/smoke/runs/{run_id}/rows", "dave", page_size=5).json()
    cursor = first["next_cursor"]
    assert cursor
    other_user = get(client, f"/api/v1/smoke/runs/{run_id}/rows", "bob", page_size=5, cursor=cursor)
    assert other_user.status_code == 400
    assert "different user" in other_user.json()["detail"]
    other_filters = get(client, f"/api/v1/smoke/runs/{run_id}/rows", "dave", page_size=5, cursor=cursor, currency="USD")
    assert other_filters.status_code == 400
    forged = get(client, f"/api/v1/smoke/runs/{run_id}/rows", "dave", page_size=5, cursor=cursor[:-3] + "abc")
    assert forged.status_code == 400


def test_row_filters(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> None:
    run_id = succeeded(client, relay, ctx, fake, lake)
    rows = load_rows(lake, run_id)
    body = get(
        client,
        f"/api/v1/smoke/runs/{run_id}/rows",
        "dave",
        page_size=500,
        currency="EUR",
        min_amount=-100,
        max_amount=100,
    ).json()
    expected = {
        r["row_id"] for r in rows
        if r["entity_id"] in {"US01", "EU01"} and r["currency"] == "EUR" and -100 <= int(str(r["amount_minor"])) < 100
    }  # fmt: skip
    assert {r["row_id"] for r in body["rows"]} == expected
    prefix = str(rows[0]["row_id"])[:1]
    by_prefix = get(client, f"/api/v1/smoke/runs/{run_id}/rows", "dave", page_size=500, row_prefix=prefix).json()
    assert by_prefix["rows"]
    assert all(str(r["row_id"]).startswith(prefix) for r in by_prefix["rows"])


def test_totals_are_scoped_to_the_caller(
    client: TestClient, relay: Relay, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend
) -> None:
    run_id = succeeded(client, relay, ctx, fake, lake)
    rows = load_rows(lake, run_id)
    alice = get(client, f"/api/v1/smoke/runs/{run_id}/totals", "alice").json()["groups"]
    assert {g["entity_id"] for g in alice} == {"US01"}
    assert sum(g["rows"] for g in alice) == sum(1 for r in rows if r["entity_id"] == "US01")
    assert sum(g["amount_minor_total"] for g in alice) == sum(
        int(str(r["amount_minor"])) for r in rows if r["entity_id"] == "US01"
    )
    assert get(client, f"/api/v1/smoke/runs/{run_id}/totals", "carol").json()["groups"] == []


def test_results_are_only_readable_once_the_run_has_succeeded(client: TestClient) -> None:
    run_id = start(client).json()["run_id"]
    for path in ("rows", "totals"):
        r = get(client, f"/api/v1/smoke/runs/{run_id}/{path}", "alice")
        assert r.status_code == 409
    assert get(client, f"/api/v1/smoke/runs/{uuid.uuid4()}/rows", "alice").status_code == 404
    assert get(client, f"/api/v1/smoke/runs/{run_id}/rows", "alice", page_size=501).status_code == 422


class _Failing:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def run(self, *_: object) -> None:
        raise self.exc


@pytest.mark.parametrize(("exc", "status"), [(QueryBusyError("busy"), 503), (QueryTimeoutError("slow"), 504)])
def test_read_path_failures_map_to_retryable_statuses(
    engine: Any,
    settings: Settings,
    fake: FakeComputeBackend,
    exc: Exception,
    status: int,
    relay: Relay,
    lake: ReferenceBackend,
) -> None:
    ctx = build_context(settings, engine=engine, compute=fake, query=_Failing(exc))  # type: ignore[arg-type]
    with TestClient(create_app(context=ctx)) as client:
        run_id = run_to(client, relay, ctx, fake, JobStatus.SUCCEEDED)
        assert get(client, f"/api/v1/smoke/runs/{run_id}/rows", "alice").status_code == status


def test_without_a_read_path_results_are_503(
    engine: Any, settings: Settings, fake: FakeComputeBackend, relay: Relay
) -> None:
    ctx = build_context(settings, engine=engine, compute=fake)
    with TestClient(create_app(context=ctx)) as client:
        run_id = run_to(client, relay, ctx, fake, JobStatus.SUCCEEDED)
        assert get(client, f"/api/v1/smoke/runs/{run_id}/totals", "alice").status_code == 503
