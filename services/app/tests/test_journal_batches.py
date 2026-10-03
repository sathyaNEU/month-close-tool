"""Journal batches (Gate 7a): members move together, never partly; the batch needs the most levels any member
needs; separation of duties covers every member's preparer; and cancelling a batch frees its journals."""

from __future__ import annotations

from typing import Any

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from journalkit import APPROVER_1, APPROVER_2, APPROVER_3, PREPARER, PREPARER_2, create, get, h, open_period, set_up
from sqlalchemy import Engine

pytestmark = pytest.mark.pg


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)
    open_period(engine)


def new_batch(client: TestClient, user: str = PREPARER) -> dict[str, Any]:
    r = client.post(
        "/api/v1/journal-batches", json={"entity_id": "US01", "description": "September close"}, headers=h(user)
    )
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def add(client: TestClient, batch: dict[str, Any], journal: dict[str, Any], user: str = PREPARER) -> Any:
    url = f"/api/v1/journal-batches/{batch['batch_id']}/journals"
    return client.post(url, json={"journal_id": journal["journal_id"]}, headers=h(user, batch["etag"]))


def act(client: TestClient, batch: dict[str, Any], verb: str, user: str, **payload: Any) -> Any:
    url = f"/api/v1/journal-batches/{batch['batch_id']}:{verb}"
    return client.post(url, json=payload or None, headers=h(user, batch["etag"]))


def acted(client: TestClient, batch: dict[str, Any], verb: str, user: str, **payload: Any) -> dict[str, Any]:
    r = act(client, batch, verb, user, **payload)
    assert r.status_code == 200, (verb, r.text)
    out: dict[str, Any] = r.json()
    return out


def test_a_batch_is_approved_all_or_none(client: TestClient, ready: None) -> None:
    batch = new_batch(client)
    small, large = create(client), create(client, lines=(("5020", "DR", 2_000_000), ("2410", "CR", 2_000_000)))
    batch = add(client, batch, small).json()
    batch = add(client, batch, large).json()
    assert act(client, batch, "approve", APPROVER_1).status_code == 409  # still a draft
    from_member = client.post(
        f"/api/v1/journals/{small['journal_id']}:submit", headers=h(PREPARER, get(client, small)["etag"])
    )
    assert (from_member.status_code, from_member.json()["title"]) == (409, "The journal is part of a batch")
    batch = acted(client, batch, "submit", PREPARER)
    assert (batch["required_levels"], batch["current_level"]) == (2, 1)  # the large journal sets it
    assert {j["required_levels"] for j in batch["journals"]} == {2}
    batch = acted(client, batch, "approve", APPROVER_3)  # level 1, by an L3 approver
    assert {(j["status"], j["current_level"]) for j in batch["journals"]} == {("pending_approval", 2)}
    assert act(client, batch, "approve", APPROVER_1).status_code == 403  # level 2 needs L2 authority
    again = act(client, batch, "approve", APPROVER_3)
    assert (again.status_code, again.json()["type"]) == (409, "urn:close:problem:sod-violation")
    assert "already approved level 1" in again.json()["detail"]
    batch = acted(client, batch, "approve", APPROVER_2)
    assert batch["status"] == "approved"
    assert {j["status"] for j in batch["journals"]} == {"approved"}
    for member in (small, large):
        approvals = get(client, member)["approvals"]
        assert [(a["level"], a["approver"], a["batch_id"]) for a in approvals] == [
            (1, APPROVER_3, batch["batch_id"]),
            (2, APPROVER_2, batch["batch_id"]),
        ]


def test_separation_of_duties_covers_every_member(client: TestClient, engine: Engine, ready: None) -> None:
    assign(engine, PREPARER_2, "journal_approver_l3", "US01")
    batch = new_batch(client)
    batch = add(client, batch, create(client)).json()
    batch = add(client, batch, create(client, user=PREPARER_2), user=PREPARER_2).json()
    batch = acted(client, batch, "submit", PREPARER)
    refused = act(client, batch, "approve", PREPARER_2)  # prepared one of the members
    assert (refused.status_code, refused.json()["type"]) == (409, "urn:close:problem:sod-violation")
    assert act(client, batch, "submit", PREPARER_2).status_code == 409  # already submitted


def test_reject_recall_and_cancel(client: TestClient, ready: None) -> None:
    batch = new_batch(client)
    first, second = create(client), create(client)
    batch = add(client, batch, first).json()
    batch = add(client, batch, second).json()
    batch = acted(client, batch, "submit", PREPARER)
    assert act(client, batch, "reject", APPROVER_1).status_code == 422  # a reason is required
    batch = acted(client, batch, "reject", APPROVER_1, reason="split the carrier charges")
    assert {j["status"] for j in batch["journals"]} == {"draft"}
    batch = acted(client, batch, "submit", PREPARER)
    assert act(client, batch, "recall", PREPARER_2).status_code == 409  # only its creator
    batch = acted(client, batch, "recall", PREPARER)
    assert (batch["status"], batch["submission"]) == ("draft", 2)
    url = f"/api/v1/journal-batches/{batch['batch_id']}/journals/{second['journal_id']}:remove"
    batch = client.post(url, headers=h(PREPARER, batch["etag"])).json()
    assert [j["journal_id"] for j in batch["journals"]] == [first["journal_id"]]
    cancelled = acted(client, batch, "cancel", PREPARER)
    assert (cancelled["status"], cancelled["journals"]) == ("cancelled", [])
    freed = get(client, first)
    assert (freed["status"], freed["batch_id"]) == ("draft", None)
    empty = new_batch(client)
    assert act(client, empty, "submit", PREPARER).status_code == 409  # nothing to submit


BATCH_STATES = ("draft", "pending_approval", "approved", "cancelled")
BATCH_VERBS = ("submit", "approve", "reject", "recall", "cancel")
BATCH_LEGAL = {
    ("draft", "submit"),
    ("draft", "cancel"),
    ("pending_approval", "approve"),
    ("pending_approval", "reject"),
    ("pending_approval", "recall"),
}
BATCH_ACTOR = {"submit": PREPARER, "approve": APPROVER_1, "reject": APPROVER_1, "recall": PREPARER, "cancel": PREPARER}


def batch_in_state(client: TestClient, state: str) -> dict[str, Any]:
    batch = add(client, new_batch(client), create(client)).json()
    if state == "draft":
        return batch
    if state == "cancelled":
        return acted(client, batch, "cancel", PREPARER)
    batch = acted(client, batch, "submit", PREPARER)
    return batch if state == "pending_approval" else acted(client, batch, "approve", APPROVER_1)


def test_every_batch_move_from_every_state(client: TestClient, ready: None) -> None:
    for state in BATCH_STATES:
        for verb in BATCH_VERBS:
            batch = batch_in_state(client, state)
            extra = {"reason": "not this month"} if verb == "reject" else {}
            r = act(client, batch, verb, BATCH_ACTOR[verb], **extra)
            expected = 200 if (state, verb) in BATCH_LEGAL else 409
            assert r.status_code == expected, (state, verb, r.text)
            if expected == 409:
                assert r.json()["title"].startswith(f"Cannot {verb}"), (state, verb, r.json())
