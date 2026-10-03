"""Maker-checker configuration versions over the API (Gate 4): the full life cycle, separation of duties,
references between configurations, ETags, idempotent replays, validation and visibility."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from close_app import audit
from close_contracts.default_sources import BY_ID as SOURCES
from close_contracts.templates import BY_ID as TEMPLATES

pytestmark = pytest.mark.pg

AUTHOR, APPROVER, OUTSIDER, READER = "2001", "2002", "2003", "2004"


@pytest.fixture
def people(engine: Engine) -> None:
    for scope in ("US01", "GLOBAL"):
        assign(engine, AUTHOR, "config_author", scope)
        assign(engine, APPROVER, "config_approver", scope)
    assign(engine, OUTSIDER, "config_author", "EU01")
    assign(engine, READER, "auditor", "US01")


def h(user: str, *, key: bool = True, if_match: str | None = None) -> dict[str, str]:
    headers = {"X-Forwarded-User": user, "X-Forwarded-Email": f"{user}@example.com"}
    if key:
        headers["Idempotency-Key"] = f"key-{uuid.uuid4()}"
    if if_match is not None:
        headers["If-Match"] = if_match
    return headers


def source_spec(source_id: str) -> dict[str, Any]:
    return SOURCES[source_id].model_dump(mode="json")


def match_set_spec() -> dict[str, Any]:
    spec = TEMPLATES["t1_orders_stripe"].model_dump(mode="json")
    return spec | {"entity_ids": ["US01"]}


def create(client: TestClient, resource: str, spec: dict[str, Any], day: str = "2026-10-01", user: str = AUTHOR) -> Any:
    return client.post(f"/api/v1/{resource}", json={"effective_from": day, "spec": spec}, headers=h(user))


def step(client: TestClient, version: dict[str, Any], verb: str, user: str, **body: Any) -> Any:
    resource = "data-sources" if version["kind"] == "data_source" else "match-sets"
    url = f"/api/v1/{resource}/{version['config_id']}/versions/{version['version']}:{verb}"
    return client.post(url, json=body or None, headers=h(user, if_match=version["etag"]))


def activate(client: TestClient, resource: str, spec: dict[str, Any], day: str = "2026-10-01") -> dict[str, Any]:
    v = create(client, resource, spec, day).json()
    v = step(client, v, "submit", AUTHOR).json()
    v = step(client, v, "approve", APPROVER).json()
    v = step(client, v, "activate", APPROVER).json()
    assert v["status"] == "active", v
    return v


def _verify(engine: Engine) -> audit.Verdict:
    with engine.connect() as conn:
        return audit.verify(conn)


def test_a_match_set_goes_from_draft_to_active_and_is_superseded(
    client: TestClient, engine: Engine, people: None
) -> None:
    draft = create(client, "match-sets", match_set_spec()).json()
    assert step(client, draft, "submit", AUTHOR).status_code == 409  # its data sources do not exist yet
    for source_id in ("orders", "stripe_balance_transactions"):
        activate(client, "data-sources", source_spec(source_id))
    v1 = step(client, draft, "submit", AUTHOR).json()
    v1 = step(client, v1, "approve", APPROVER).json()
    v1 = step(client, v1, "activate", APPROVER).json()
    assert (v1["status"], v1["approved_by"], v1["activated_by"], v1["authors"]) == (
        "active",
        APPROVER,
        APPROVER,
        [AUTHOR],
    )

    r = client.post(
        f"/api/v1/match-sets/{v1['config_id']}/versions",
        json={"effective_from": "2026-11-01", "spec": match_set_spec()},
        headers=h(AUTHOR),
    )
    assert r.status_code == 201
    v2 = r.json()
    assert v2["version"] == 2
    v2 = step(client, v2, "submit", AUTHOR).json()
    v2 = step(client, v2, "approve", APPROVER).json()
    v2 = step(client, v2, "activate", APPROVER).json()
    versions = client.get(f"/api/v1/match-sets/{v1['config_id']}/versions", headers=h(READER, key=False)).json()
    assert [(v["version"], v["status"], v["effective_to"]) for v in versions["items"]] == [
        (1, "retired", "2026-11-01"),
        (2, "active", None),
    ]
    verdict = _verify(engine)
    assert verdict.ok
    with engine.connect() as conn:
        actions = conn.execute(
            text("SELECT action FROM audit_events WHERE target LIKE 'match_set:%' ORDER BY seq")
        ).scalars()
        assert list(actions) == [
            "match_set.create",
            "match_set.submit",
            "match_set.approve",
            "match_set.activate",
            "match_set.create",
            "match_set.submit",
            "match_set.approve",
            "match_set.retire",
            "match_set.activate",
        ]


def test_separation_of_duties_is_enforced_and_audited(client: TestClient, engine: Engine, people: None) -> None:
    assign(engine, AUTHOR, "config_approver", "GLOBAL")  # even with the approver role...
    v = create(client, "data-sources", source_spec("orders")).json()
    v = step(client, v, "submit", AUTHOR).json()
    r = step(client, v, "approve", AUTHOR)  # ...the author cannot approve their own version
    assert r.status_code == 409
    assert r.json()["type"] == "urn:close:problem:sod-violation"
    v = step(client, v, "approve", APPROVER).json()
    r = step(client, v, "activate", AUTHOR)  # ...nor activate it
    assert r.status_code == 409
    with engine.connect() as conn:
        denied = conn.execute(
            text("SELECT actor, action FROM audit_events WHERE outcome = 'denied' ORDER BY seq")
        ).all()
    assert [tuple(d) for d in denied] == [(AUTHOR, "config.approve"), (AUTHOR, "config.activate")]


def test_an_editor_becomes_an_author(client: TestClient, engine: Engine, people: None) -> None:
    assign(engine, APPROVER, "config_author", "GLOBAL")
    v = create(client, "data-sources", source_spec("orders")).json()
    r = client.put(
        f"/api/v1/data-sources/orders/versions/{v['version']}",
        json={"effective_from": "2026-10-02", "spec": source_spec("orders")},
        headers=h(APPROVER, key=False, if_match=v["etag"]),
    )
    assert r.status_code == 200
    v = r.json()
    assert v["authors"] == [AUTHOR, APPROVER]
    v = step(client, v, "submit", AUTHOR).json()
    assert step(client, v, "approve", APPROVER).status_code == 409


def test_if_match_is_required_and_must_be_current(client: TestClient, people: None) -> None:
    v = create(client, "data-sources", source_spec("orders")).json()
    url = "/api/v1/data-sources/orders/versions/1"
    body = {"effective_from": "2026-10-05", "spec": source_spec("orders")}
    assert client.put(url, json=body, headers=h(AUTHOR, key=False)).status_code == 428
    first = client.put(url, json=body, headers=h(AUTHOR, key=False, if_match=v["etag"]))
    assert first.status_code == 200
    assert first.headers["ETag"] != v["etag"]
    stale = client.put(url, json=body, headers=h(AUTHOR, key=False, if_match=v["etag"]))
    assert stale.status_code == 412
    assert stale.json()["current_etag"] == first.headers["ETag"]
    assert client.get(url, headers=h(AUTHOR, key=False)).headers["ETag"] == first.headers["ETag"]


def test_a_replay_after_a_later_change_returns_the_stored_response(client: TestClient, people: None) -> None:
    v = create(client, "data-sources", source_spec("orders")).json()
    headers = h(AUTHOR, if_match=v["etag"])
    submitted = client.post("/api/v1/data-sources/orders/versions/1:submit", headers=headers)
    approved = step(client, submitted.json(), "approve", APPROVER)  # the version changes after the submit
    assert approved.status_code == 200
    replay = client.post("/api/v1/data-sources/orders/versions/1:submit", headers=headers)
    assert replay.status_code == 200
    assert replay.content == submitted.content
    assert replay.headers["Idempotent-Replayed"] == "true"


def test_one_version_in_progress_and_ordered_effective_dates(client: TestClient, people: None) -> None:
    activate(client, "data-sources", source_spec("orders"))
    assert create(client, "data-sources", source_spec("orders"), "2026-10-01").status_code == 201  # v2 draft
    assert create(client, "data-sources", source_spec("orders"), "2026-12-01").status_code == 409  # v3 while v2 open
    v2 = client.get("/api/v1/data-sources/orders/versions/2", headers=h(AUTHOR, key=False)).json()
    v2 = step(client, v2, "submit", AUTHOR).json()
    v2 = step(client, v2, "approve", APPROVER).json()
    r = step(client, v2, "activate", APPROVER)  # the same effective date as the active version
    assert r.status_code == 409
    assert "take effect after" in r.json()["title"]


def test_reject_returns_to_draft_and_needs_a_reason(client: TestClient, people: None) -> None:
    v = create(client, "data-sources", source_spec("orders")).json()
    v = step(client, v, "submit", AUTHOR).json()
    assert step(client, v, "reject", APPROVER).status_code == 422
    rejected = step(client, v, "reject", APPROVER, reason="the filter is missing")
    assert rejected.status_code == 200
    assert (rejected.json()["status"], rejected.json()["submitted_by"]) == ("draft", None)


def test_retire_needs_a_later_effective_to(client: TestClient, people: None) -> None:
    v = activate(client, "data-sources", source_spec("orders"))
    assert step(client, v, "retire", APPROVER, effective_to="2026-09-01").status_code == 422
    retired = step(client, v, "retire", APPROVER, effective_to="2027-01-01")
    assert retired.json()["status"] == "retired"


def test_a_data_source_an_active_match_set_uses_cannot_be_retired(client: TestClient, people: None) -> None:
    orders = activate(client, "data-sources", source_spec("orders"))
    activate(client, "data-sources", source_spec("stripe_balance_transactions"))
    match_set = activate(client, "match-sets", match_set_spec())
    refused = step(client, orders, "retire", APPROVER, effective_to="2027-01-01")
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("active match sets use it: t1_orders_stripe;")
    assert step(client, match_set, "retire", APPROVER, effective_to="2027-01-01").status_code == 200
    assert step(client, orders, "retire", APPROVER, effective_to="2027-01-01").json()["status"] == "retired"


def test_invalid_specifications_are_problems_with_issues(client: TestClient, people: None) -> None:
    bad = source_spec("orders") | {"mapping": {"entity_id": {"col": "no_such_column"}}}
    r = create(client, "data-sources", bad)
    assert r.status_code == 422
    assert r.json()["type"] == "urn:close:problem:validation-failed"
    assert r.json()["issues"]
    wrong_id = client.post(
        "/api/v1/data-sources/other_source/versions",
        json={"effective_from": "2026-10-01", "spec": source_spec("orders")},
        headers=h(AUTHOR),
    )
    assert wrong_id.status_code == 422


def test_visibility_and_scope(client: TestClient, people: None) -> None:
    for source_id in ("orders", "stripe_balance_transactions"):
        activate(client, "data-sources", source_spec(source_id))
    create(client, "match-sets", match_set_spec())
    ms = "/api/v1/match-sets/t1_orders_stripe/versions/1"
    assert client.get(ms, headers=h(OUTSIDER, key=False)).status_code == 404  # an EU01 author cannot see US01
    assert client.get(ms, headers=h(READER, key=False)).status_code == 200
    assert client.get("/api/v1/match-sets", headers=h(OUTSIDER, key=False)).json()["items"] == []
    assert create(client, "data-sources", source_spec("tax_documents"), user=OUTSIDER).status_code == 403  # not GLOBAL
    outside = match_set_spec() | {"match_set_id": "eu_set"}
    assert create(client, "match-sets", outside, user=OUTSIDER).status_code == 403  # an author in EU01, not US01


def test_the_database_refuses_an_author_as_approver(engine: Engine, client: TestClient, people: None) -> None:
    create(client, "data-sources", source_spec("orders"))
    with pytest.raises(IntegrityError), engine.begin() as tx:
        tx.execute(text("UPDATE config_versions SET approved_by = created_by"))


def test_a_multi_entity_match_set_is_read_with_one_entity_and_changed_with_all(
    client: TestClient, engine: Engine, people: None
) -> None:
    """t1_orders_stripe spans US01 and UK01: reading it needs a role in one of them, every change needs all."""
    for source_id in ("orders", "stripe_balance_transactions"):
        activate(client, "data-sources", source_spec(source_id))
    both = TEMPLATES["t1_orders_stripe"].model_dump(mode="json")
    assert both["entity_ids"] == ["US01", "UK01"]
    assert create(client, "match-sets", both).status_code == 403  # AUTHOR is an author in US01 only
    assign(engine, AUTHOR, "config_author", "UK01")
    v = create(client, "match-sets", both).json()
    url = "/api/v1/match-sets/t1_orders_stripe/versions/1"

    def listed(user: str) -> bool:
        items = client.get("/api/v1/match-sets", headers=h(user, key=False)).json()["items"]
        return "t1_orders_stripe" in [i["config_id"] for i in items]

    assert client.get(url, headers=h(READER, key=False)).status_code == 200  # a reader in US01 only
    assert listed(READER)
    assert client.get(url, headers=h(OUTSIDER, key=False)).status_code == 404  # EU01 only: not one of its entities
    assert not listed(OUTSIDER)

    assign(engine, OUTSIDER, "config_author", "US01")  # now an author in US01 and EU01, but not UK01
    assert client.get(url, headers=h(OUTSIDER, key=False)).status_code == 200
    edit = {"effective_from": "2026-10-02", "spec": both}
    assert client.put(url, json=edit, headers=h(OUTSIDER, key=False, if_match=v["etag"])).status_code == 403
    assert step(client, v, "submit", OUTSIDER).status_code == 403
    v = step(client, v, "submit", AUTHOR).json()
    assert step(client, v, "approve", APPROVER).status_code == 403  # an approver in US01 only
    assign(engine, APPROVER, "config_approver", "UK01")
    v = step(client, v, "approve", APPROVER).json()
    assert step(client, v, "activate", APPROVER).json()["status"] == "active"
