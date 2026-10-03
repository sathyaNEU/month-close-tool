"""The journal workflow through the API (Gate 7a): every state against every move, separation of duties and
delegation, routing frozen at submit, the risk score, reversals, validation and attachments."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from journalkit import (
    APPROVER_1,
    APPROVER_2,
    APPROVER_3,
    CONTROLLER,
    OUTSIDER,
    PREPARER,
    PREPARER_2,
    activate,
    approved,
    body,
    create,
    get,
    h,
    moved,
    open_period,
    set_up,
    step,
)
from sqlalchemy import Engine, text

from close_app.journals import attachments
from close_contracts.config.envelope import ConfigKind
from close_contracts.samples.journal_workflow import SAMPLE_APPROVAL_MATRIX

pytestmark = pytest.mark.pg

STATES = ("draft", "pending_approval", "approved", "cancelled")
VERBS = ("submit", "approve", "reject", "recall", "cancel", "reverse")
LEGAL = {
    ("draft", "submit"),
    ("draft", "cancel"),
    ("pending_approval", "approve"),
    ("pending_approval", "reject"),
    ("pending_approval", "recall"),
    ("approved", "reverse"),
}
ACTOR = {"submit": PREPARER, "approve": APPROVER_1, "reject": APPROVER_1, "recall": PREPARER, "cancel": PREPARER}
ACTOR["reverse"] = PREPARER
TWO_LEVELS = (("5020", "DR", 2_000_000), ("2410", "CR", 2_000_000))


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)
    open_period(engine)
    open_period(engine, period="2026-10")


def in_state(client: TestClient, state: str) -> dict[str, Any]:
    j = create(client)
    if state == "draft":
        return j
    if state == "cancelled":
        return moved(client, j, "cancel", PREPARER)
    j = moved(client, j, "submit", PREPARER)
    return j if state == "pending_approval" else moved(client, j, "approve", APPROVER_1)


def denials(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT action, target FROM audit_events WHERE outcome = 'denied' ORDER BY seq"))
        return [(str(a), str(t)) for a, t in rows]


def test_every_move_from_every_state(client: TestClient, ready: None) -> None:
    for state in STATES:
        for verb in VERBS:
            j = in_state(client, state)
            r = step(client, j, verb, ACTOR[verb], **({"reason": "the amount is wrong"} if verb == "reject" else {}))
            expected = (201 if verb == "reverse" else 200) if (state, verb) in LEGAL else 409
            assert r.status_code == expected, (state, verb, r.text)
            if expected == 409:
                assert r.json()["title"].startswith(f"Cannot {verb}"), (state, verb, r.json())
        j = in_state(client, state)
        edit = client.put(
            f"/api/v1/journals/{j['journal_id']}",
            json=body(description="An edited, longer description"),
            headers=h(PREPARER, j["etag"]),
        )
        assert edit.status_code == (200 if state == "draft" else 409), (state, edit.text)


def test_moves_need_the_current_etag(client: TestClient, ready: None) -> None:
    j = create(client)
    no_etag = client.post(f"/api/v1/journals/{j['journal_id']}:submit", headers=h(PREPARER))
    assert no_etag.status_code == 428
    edited = client.put(
        f"/api/v1/journals/{j['journal_id']}",
        json=body(description="A second description"),
        headers=h(PREPARER, j["etag"]),
    )
    assert edited.status_code == 200
    assert step(client, j, "submit", PREPARER).status_code == 412  # the old ETag
    assert edited.json()["revision"] == 2
    assert moved(client, edited.json(), "submit", PREPARER)["status"] == "pending_approval"


def test_separation_of_duties(client: TestClient, engine: Engine, ready: None) -> None:
    assign(engine, PREPARER, "journal_approver_l3", "US01")  # even with approval authority...
    j = moved(client, create(client), "submit", PREPARER)
    refused = step(client, j, "approve", PREPARER)
    assert (refused.status_code, refused.json()["type"]) == (409, "urn:close:problem:sod-violation")
    assert step(client, j, "reject", PREPARER, reason="mine").status_code == 409  # ...the preparer can't reject either
    big = moved(client, create(client, lines=TWO_LEVELS), "submit", PREPARER)
    assert big["required_levels"] == 2
    big = moved(client, big, "approve", APPROVER_3)  # level 1, by an L3 approver
    again = step(client, big, "approve", APPROVER_3)
    assert again.status_code == 409
    assert f"{APPROVER_3} already approved level 1" in again.json()["detail"]
    assert step(client, big, "approve", APPROVER_1).status_code == 403  # no level 2 authority
    big = moved(client, big, "approve", APPROVER_2)
    assert big["status"] == "approved"
    assert [(a["level"], a["approver"]) for a in big["approvals"]] == [(1, APPROVER_3), (2, APPROVER_2)]
    assert [a for a, _ in denials(engine)] == ["journal.approve.l1"] * 2 + ["journal.approve.l2"] * 2
    other = create(client)
    assert step(client, other, "submit", PREPARER_2).status_code == 409  # only the preparer submits
    assert denials(engine)[-1] == ("journal.prepare", f"journal:US01:{other['number']}")


def test_delegation(client: TestClient, engine: Engine, ready: None) -> None:
    window = {"starts_on": "2026-01-01", "ends_on": "2027-12-31"}
    given = client.post(
        "/api/v1/delegations", json={"delegate": PREPARER_2, "entity_ids": ["US01"], **window}, headers=h(APPROVER_2)
    )
    assert given.status_code == 201, given.text
    j = moved(client, create(client), "submit", PREPARER)
    j = moved(client, j, "approve", PREPARER_2)  # a preparer with no approver role, standing in for APPROVER_2
    assert (j["approvals"][0]["approver"], j["approvals"][0]["on_behalf_of"]) == (PREPARER_2, APPROVER_2)
    own = moved(client, create(client, user=PREPARER_2), "submit", PREPARER_2)
    assert step(client, own, "approve", PREPARER_2).status_code == 409  # a delegate is still the preparer
    assign(engine, APPROVER_2, "journal_preparer", "US01")
    theirs = moved(client, create(client, user=APPROVER_2), "submit", APPROVER_2)
    refused = step(client, theirs, "approve", PREPARER_2)
    assert refused.status_code == 409
    assert f"stand in for {APPROVER_2}" in refused.json()["detail"]
    two = moved(client, create(client, lines=TWO_LEVELS), "submit", PREPARER)
    two = moved(client, two, "approve", APPROVER_2)  # the delegator approves level 1 in person...
    assert "already approved" in step(client, two, "approve", PREPARER_2).json()["detail"]  # ...so not level 2 again
    self_grant = client.post(
        "/api/v1/delegations", json={"delegate": APPROVER_2, "entity_ids": ["US01"], **window}, headers=h(APPROVER_2)
    )
    assert self_grant.status_code == 409
    blind = client.post(
        "/api/v1/delegations", json={"delegate": OUTSIDER, "entity_ids": ["US01"], **window}, headers=h(APPROVER_2)
    )
    assert blind.status_code == 422
    listed = client.get("/api/v1/delegations", headers=h(PREPARER_2)).json()["items"]
    assert [d["delegator"] for d in listed] == [APPROVER_2]
    revoke = client.post(f"/api/v1/delegations/{given.json()['delegation_id']}:revoke", headers=h(APPROVER_2))
    assert revoke.status_code == 200
    later = moved(client, create(client), "submit", PREPARER)
    assert step(client, later, "approve", PREPARER_2).status_code == 403


def test_routing_is_frozen_at_submit(client: TestClient, engine: Engine, ready: None) -> None:
    lines = (("5020", "DR", 512_345), ("2410", "CR", 512_345))  # not round, so the risk score stays below a band
    j = moved(client, create(client, lines=lines), "submit", PREPARER)
    assert (j["required_levels"], j["matrix_version"]) == (1, 1)
    stricter = SAMPLE_APPROVAL_MATRIX.model_copy(
        update={
            "thresholds": tuple(
                t.model_copy(update={"level_2_from_minor": 100, "level_3_from_minor": 200})
                for t in SAMPLE_APPROVAL_MATRIX.thresholds
            )
        }
    )
    with engine.begin() as tx:
        tx.execute(text("UPDATE config_versions SET status = 'retired' WHERE kind = 'approval_matrix'"))
    activate(engine, ConfigKind.APPROVAL_MATRIX, SAMPLE_APPROVAL_MATRIX.matrix_id, stricter, version=2)
    j = moved(client, j, "approve", APPROVER_1)
    assert j["status"] == "approved"  # routed under version 1: one level
    assert j["approvals"][0]["content_hash"] == j["content_hash"]
    fresh = moved(client, create(client, lines=lines), "submit", PREPARER)
    assert (fresh["required_levels"], fresh["matrix_version"]) == (3, 2)
    fresh = moved(client, fresh, "approve", APPROVER_1)
    recalled = moved(client, fresh, "recall", PREPARER)
    assert recalled["approvals"] == []  # the next submission starts over
    again = moved(client, recalled, "submit", PREPARER)
    assert (again["submission"], again["current_level"], again["approvals"]) == (2, 1, [])


def test_the_risk_score_raises_the_levels(client: TestClient, ready: None) -> None:
    risky = create(
        client,
        lines=(("9999", "DR", 300_000), ("2410", "CR", 300_000)),
        accounting_date="2026-09-29",
        description="Suspense clearing",
    )
    j = moved(client, risky, "submit", PREPARER)
    hit = {name for name, f in j["risk_factors"].items() if f["hit"]}
    assert hit == {
        "seldom_used_account",
        "infrequent_preparer",
        "period_end_thin_description",
        "sensitive_account",
        "round_amount",
    }
    assert (j["risk_score"], j["required_levels"], j["routing"]["risk_levels"], j["routing"]["amount_levels"]) == (
        90,
        3,
        3,
        1,
    )


def test_reversals(client: TestClient, ready: None) -> None:
    j = approved(client, create(client))
    back = moved(client, j, "reverse", PREPARER)
    assert (back["source"], back["reverses_journal_id"], back["accounting_date"]) == (
        "reversal",
        j["journal_id"],
        "2026-10-01",
    )
    assert [(ln["account"], ln["side"]) for ln in back["lines"]] == [("5020", "CR"), ("2410", "DR")]
    assert step(client, j, "reverse", PREPARER).status_code == 409  # one live reversal
    moved(client, back, "cancel", PREPARER)
    assert moved(client, j, "reverse", PREPARER)["status"] == "draft"  # a cancelled reversal frees it


def test_an_accrual_reverses_itself_in_the_next_period(client: TestClient, engine: Engine) -> None:
    set_up(engine)
    open_period(engine)
    accrual = create(
        client, template_id="sample_accrual", header={"accrual_type": "carrier"}, description="Accrued carrier charges"
    )
    assert accrual["auto_reverse"] is True
    approved(client, accrual)
    listed = client.get("/api/v1/journals?entity_id=US01", headers=h(PREPARER)).json()["items"]
    assert len(listed) == 1  # October isn't open yet
    opened = client.post("/api/v1/periods", json={"entity_id": "US01", "period": "2026-10"}, headers=h(CONTROLLER))
    assert opened.status_code == 201, opened.text
    reversal = next(
        j
        for j in client.get("/api/v1/journals?entity_id=US01", headers=h(PREPARER)).json()["items"]
        if j["source"] == "reversal"
    )
    assert (reversal["accounting_date"], reversal["status"], reversal["auto_reverse"]) == (
        "2026-10-01",
        "approved",
        False,
    )
    assert reversal["bypass_reason"].startswith("automatic reversal")
    assert reversal["created_by"] == PREPARER
    exceptions = client.get("/api/v1/journal-exceptions?entity_id=US01", headers=h(CONTROLLER)).json()["items"]
    assert [(e["kind"], e["number"]) for e in exceptions] == [("bypass", reversal["number"])]


def test_invalid_journals_are_problems_with_issues(client: TestClient, engine: Engine, ready: None) -> None:
    r = client.post("/api/v1/journals", json=body(("5020", "DR", 10), ("2410", "CR", 9)), headers=h(PREPARER))
    assert r.status_code == 422
    assert {i["code"] for i in r.json()["issues"]} == {"unbalanced"}
    assert client.post("/api/v1/journals", json=body(), headers=h(OUTSIDER)).status_code == 404  # US01 is invisible
    closed = client.post("/api/v1/journals", json=body(accounting_date="2026-11-03"), headers=h(PREPARER))
    assert (closed.status_code, closed.json()["title"]) == (409, "The period isn't open")
    j = create(client)
    with engine.begin() as tx:
        tx.execute(text("UPDATE accounting_periods SET status = 'closed', closed_at = now() WHERE period = '2026-09'"))
    assert step(client, j, "submit", PREPARER).status_code == 409


def test_attachments(client: TestClient, ready: None, monkeypatch: pytest.MonkeyPatch) -> None:
    big = create(client, lines=(("5020", "DR", 5_000_000), ("2410", "CR", 5_000_000)))
    blocked = step(client, big, "submit", PREPARER)
    assert (blocked.status_code, blocked.json()["title"]) == (409, "An attachment is required")
    pdf = b"%PDF-1.7 supporting schedule"
    url = f"/api/v1/journals/{big['journal_id']}/attachments"

    def upload(name: str, kind: str, content: bytes) -> Any:
        payload = {"file_name": name, "content_type": kind, "content_base64": base64.b64encode(content).decode()}
        return client.post(url, json=payload, headers=h(PREPARER))

    added = upload("schedule.pdf", "application/pdf", pdf)
    assert added.status_code == 201, added.text
    assert (added.json()["sha256"], added.json()["size_bytes"]) == (hashlib.sha256(pdf).hexdigest(), len(pdf))
    assert upload("page.html", "text/html", b"<script>1</script>").status_code == 415
    assert upload("../x.pdf", "application/pdf", pdf).status_code == 422
    monkeypatch.setattr(attachments, "MAX_BYTES", 10)
    assert upload("big.pdf", "application/pdf", b"x" * 11).status_code == 413
    got = client.get(f"{url}/{added.json()['attachment_id']}", headers=h(PREPARER))
    assert got.content == pdf
    assert got.headers["content-disposition"].startswith('attachment; filename="schedule.pdf"')
    assert got.headers["x-content-type-options"] == "nosniff"
    submitted = moved(client, get(client, big), "submit", PREPARER)
    assert upload("late.pdf", "application/pdf", pdf).status_code == 409  # drafts only
    assert submitted["status"] == "pending_approval"
