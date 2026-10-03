"""Access administration and the audit log over the API (Gate 4): grants, revocation, the self-grant block,
and which audit events each principal can read."""

from __future__ import annotations

import uuid

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from close_app import audit

pytestmark = pytest.mark.pg

ADMIN, AUDITOR_US, AUDITOR_GLOBAL, NEWCOMER = "3001", "3002", "3003", "3010"


@pytest.fixture
def people(engine: Engine) -> None:
    assign(engine, ADMIN, "platform_admin", "GLOBAL")
    assign(engine, AUDITOR_US, "auditor", "US01")
    assign(engine, AUDITOR_GLOBAL, "auditor", "GLOBAL")


def h(user: str, key: str | None = None) -> dict[str, str]:
    return {
        "X-Forwarded-User": user,
        "X-Forwarded-Email": f"{user}@example.com",
        "Idempotency-Key": key or f"key-{uuid.uuid4()}",
    }


def grant(client: TestClient, user: str, role: str, scope: str, by: str = ADMIN, key: str | None = None) -> object:
    return client.post(
        "/api/v1/role-assignments", json={"user_id": user, "role": role, "scope": scope}, headers=h(by, key)
    )


def test_grant_replay_duplicate_and_revoke(client: TestClient, engine: Engine, people: None) -> None:
    first = grant(client, NEWCOMER, "config_author", "US01", key="grant-key-1")
    assert first.status_code == 201  # type: ignore[attr-defined]
    replay = grant(client, NEWCOMER, "config_author", "US01", key="grant-key-1")
    assert replay.content == first.content  # type: ignore[attr-defined]
    assert grant(client, NEWCOMER, "config_author", "US01").status_code == 409  # type: ignore[attr-defined]
    me = client.get("/api/v1/whoami", headers=h(NEWCOMER)).json()
    assert (me["entities"], me["roles"]) == (["US01"], [["config_author", "US01"]])
    assignment = first.json()["assignment_id"]  # type: ignore[attr-defined]
    assert client.post(f"/api/v1/role-assignments/{assignment}:revoke", headers=h(ADMIN)).status_code == 200
    assert client.post(f"/api/v1/role-assignments/{assignment}:revoke", headers=h(ADMIN)).status_code == 404
    assert client.get("/api/v1/whoami", headers=h(NEWCOMER)).json()["entities"] == []
    with engine.connect() as conn:
        assert audit.verify(conn).ok
        actions = conn.execute(text("SELECT action FROM audit_events ORDER BY seq")).scalars().all()
    assert actions == ["access.grant", "access.revoke"]


def test_nobody_can_grant_themselves_a_role(client: TestClient, engine: Engine, people: None) -> None:
    r = grant(client, ADMIN, "config_approver", "GLOBAL", by=ADMIN)
    assert r.status_code == 409  # type: ignore[attr-defined]
    assert r.json()["type"] == "urn:close:problem:sod-violation"  # type: ignore[attr-defined]
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT actor, action, outcome FROM audit_events")).all()
    assert [tuple(row) for row in rows] == [(ADMIN, "access.grant", "denied")]


def test_only_global_access_administrators_manage_access(client: TestClient, people: None) -> None:
    assert grant(client, NEWCOMER, "auditor", "US01", by=AUDITOR_US).status_code == 403  # type: ignore[attr-defined]
    assert client.get("/api/v1/users", headers=h(AUDITOR_US)).status_code == 403
    assert client.get("/api/v1/users", headers=h(AUDITOR_GLOBAL)).status_code == 200
    listed = client.get("/api/v1/role-assignments", params={"user_id": AUDITOR_US}, headers=h(ADMIN)).json()
    assert [(a["role"], a["scope"]) for a in listed["items"]] == [("auditor", "US01")]


def test_audit_events_are_visible_by_entity_and_all_of_them_globally(
    client: TestClient, engine: Engine, people: None
) -> None:
    with engine.begin() as tx:
        audit.append(tx, audit.Event("x", "test.us", entity_ids=("US01",)))
        audit.append(tx, audit.Event("x", "test.eu", entity_ids=("EU01",)))
        audit.append(tx, audit.Event("x", "test.both", entity_ids=("EU01", "US01")))
        audit.append(tx, audit.Event("x", "test.global"))

    def seen(user: str) -> list[str]:
        return [e["action"] for e in client.get("/api/v1/audit-events", headers=h(user)).json()["items"]]

    assert seen(AUDITOR_US) == ["test.us", "test.both"]
    assert seen(AUDITOR_GLOBAL) == ["test.us", "test.eu", "test.both", "test.global"]  # the whole trail
    assert client.get("/api/v1/audit-events", headers=h(NEWCOMER)).status_code == 403
    page = client.get("/api/v1/audit-events", params={"limit": 1}, headers=h(AUDITOR_US)).json()
    assert page["next_after"] == 1
    rest = client.get("/api/v1/audit-events", params={"after": 1}, headers=h(AUDITOR_US)).json()
    assert [e["action"] for e in rest["items"]] == ["test.both"]


def test_the_role_catalog_is_public_to_signed_in_principals(client: TestClient) -> None:
    roles = client.get("/api/v1/roles", headers=h(NEWCOMER)).json()["items"]
    assert {r["role"] for r in roles} >= {"platform_admin", "auditor", "journal_approver_l3"}
    assert client.get("/api/v1/roles").status_code == 401
