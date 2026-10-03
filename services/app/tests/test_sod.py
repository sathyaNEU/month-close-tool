"""Separation of duties (Gate 4): every Stage 4 scenario is blocked, and every blocked attempt is audited.

The roadmap's other scenarios use the same mechanism when their objects exist: approving your own manual
match (Stage 5), and preparing and posting the same journal (Stage 7).
"""

from __future__ import annotations

from typing import Any

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from test_configs_api import APPROVER, AUTHOR, create, h, match_set_spec, people, source_spec, step  # noqa: F401

pytestmark = pytest.mark.pg


def _denied(engine: Engine) -> list[tuple[str, str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT actor, action, target FROM audit_events WHERE outcome = 'denied' ORDER BY seq")
        )
        return [tuple(r) for r in rows]


def _blocked(response: Any) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["type"] == "urn:close:problem:sod-violation"


def _ready_sources(client: TestClient) -> None:
    for source_id in ("orders", "stripe_balance_transactions"):
        v = create(client, "data-sources", source_spec(source_id)).json()
        v = step(client, v, "submit", AUTHOR).json()
        v = step(client, v, "approve", APPROVER).json()
        step(client, v, "activate", APPROVER)


@pytest.fixture
def author_can_approve(engine: Engine, people: None) -> None:  # noqa: F811
    for scope in ("US01", "GLOBAL"):
        assign(engine, AUTHOR, "config_approver", scope)


@pytest.mark.parametrize(("resource", "spec"), [("data-sources", "orders"), ("match-sets", "t1")])
def test_sod_approving_your_own_configuration_change(
    client: TestClient, engine: Engine, author_can_approve: None, resource: str, spec: str
) -> None:
    if resource == "match-sets":
        _ready_sources(client)
    body = source_spec("orders") if spec == "orders" else match_set_spec()
    v = create(client, resource, body).json()
    v = step(client, v, "submit", AUTHOR).json()
    _blocked(step(client, v, "approve", AUTHOR))
    assert _denied(engine)[-1][:2] == (AUTHOR, "config.approve")


@pytest.mark.parametrize(("resource", "spec"), [("data-sources", "orders"), ("match-sets", "t1")])
def test_sod_activating_your_own_rule(
    client: TestClient, engine: Engine, author_can_approve: None, resource: str, spec: str
) -> None:
    if resource == "match-sets":
        _ready_sources(client)
    body = source_spec("orders") if spec == "orders" else match_set_spec()
    v = create(client, resource, body).json()
    v = step(client, v, "submit", AUTHOR).json()
    v = step(client, v, "approve", APPROVER).json()
    _blocked(step(client, v, "activate", AUTHOR))
    assert _denied(engine)[-1][:2] == (AUTHOR, "config.activate")
    assert step(client, v, "activate", APPROVER).json()["status"] == "active"  # the approver may activate


def test_sod_an_editor_is_an_author(client: TestClient, engine: Engine, people: None) -> None:  # noqa: F811
    assign(engine, APPROVER, "config_author", "GLOBAL")
    v = create(client, "data-sources", source_spec("orders")).json()
    edited = client.put(
        "/api/v1/data-sources/orders/versions/1",
        json={"effective_from": "2026-10-03", "spec": source_spec("orders")},
        headers=h(APPROVER, key=False, if_match=v["etag"]),
    ).json()
    v = step(client, edited, "submit", AUTHOR).json()
    _blocked(step(client, v, "approve", APPROVER))


def test_sod_granting_yourself_a_role(client: TestClient, engine: Engine) -> None:
    assign(engine, "admin-1", "platform_admin", "GLOBAL")
    r = client.post(
        "/api/v1/role-assignments",
        json={"user_id": "admin-1", "role": "config_approver", "scope": "US01"},
        headers=h("admin-1"),
    )
    _blocked(r)
    assert _denied(engine) == [("admin-1", "access.grant", "access:admin-1:config_approver:US01")]
