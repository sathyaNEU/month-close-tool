"""The journal workflow's scheduled tasks (Gate 7a), driven by an as-of time instead of sleeping: recurring
journals once per period, reminders and escalation at the matrix's intervals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    create,
    h,
    moved,
    open_period,
    set_up,
)
from sqlalchemy import Engine, text

from close_app.journals import tasks
from close_contracts.config.envelope import ConfigKind
from close_contracts.samples.journal_workflow import SAMPLE_RECURRING

pytestmark = pytest.mark.pg


def notes(engine: Engine, kind: str | None = None) -> list[tuple[str, str]]:
    sql = "SELECT user_id, dedup_key FROM notifications" + (" WHERE kind = :k" if kind else "") + " ORDER BY dedup_key"
    with engine.connect() as conn:
        return [(str(u), str(k)) for u, k in conn.execute(text(sql), {"k": kind})]


def test_recurring_journals_are_created_once_per_period(client: TestClient, engine: Engine) -> None:
    set_up(engine)
    open_period(engine)  # September only
    monthly = SAMPLE_RECURRING[0].model_copy(update={"preparer": PREPARER})
    quarterly = SAMPLE_RECURRING[1].model_copy(update={"preparer": OUTSIDER})
    activate(engine, ConfigKind.RECURRING_JOURNAL, monthly.recurring_id, monthly)
    activate(engine, ConfigKind.RECURRING_JOURNAL, quarterly.recurring_id, quarterly)
    assert tasks.run_recurring(engine, as_of=datetime(2026, 9, 27, 12, tzinfo=UTC)) == 0  # September's day is the 28th
    at = datetime(2026, 9, 29, 12, tzinfo=UTC)
    assert tasks.run_recurring(engine, as_of=at) == 1
    assert tasks.run_recurring(engine, as_of=at) == 0  # once per period, however often it runs
    items = client.get("/api/v1/journals?entity_id=US01", headers=h(PREPARER)).json()["items"]
    assert [(j["recurring_period"], j["source"], j["status"], j["created_by"]) for j in items] == [
        ("2026-09", "recurring", "approved", PREPARER)
    ]
    assert items[0]["bypass_reason"].startswith("recurring journal")
    skipped = [key for _, key in notes(engine, "recurring_skipped")]
    assert skipped == [
        "recurring_skipped:sample_dispute_fee_accrual:2026-07",  # EU01 has no open period
        "recurring_skipped:sample_reserve_top_up:2026-07",
        "recurring_skipped:sample_reserve_top_up:2026-08",
    ]
    tasks.run_recurring(engine, as_of=at)
    assert len(notes(engine, "recurring_skipped")) == 3  # each skip is reported once
    kinds = [
        e["kind"]
        for e in client.get("/api/v1/journal-exceptions?entity_id=US01", headers=h(CONTROLLER)).json()["items"]
    ]
    assert sorted(kinds) == ["bypass", "recurring_skipped", "recurring_skipped"]


def _submitted_at(engine: Engine, journal: dict[str, Any]) -> datetime:
    with engine.connect() as conn:
        since: datetime = conn.execute(
            text("SELECT level_since FROM journals WHERE journal_id = :id"), {"id": journal["journal_id"]}
        ).scalar_one()
    return since


def test_reminders_and_escalation(client: TestClient, engine: Engine) -> None:
    set_up(engine)
    open_period(engine)
    waiting = moved(client, create(client), "submit", PREPARER)
    since = _submitted_at(engine, waiting)
    assert tasks.run_reminders(engine, as_of=since + timedelta(hours=23)) == 0
    level_1 = {APPROVER_1, APPROVER_2, APPROVER_3}  # L2 and L3 approvers may approve level 1
    assert tasks.run_reminders(engine, as_of=since + timedelta(hours=25)) == len(level_1)
    assert tasks.run_reminders(engine, as_of=since + timedelta(hours=25)) == 0
    assert {u for u, _ in notes(engine, "reminder")} == level_1
    # At 73 hours: reminders 2 and 3 are due, and the third escalates (after two reminders).
    written = tasks.run_reminders(engine, as_of=since + timedelta(hours=73))
    escalated = {u for u, _ in notes(engine, "escalation")}
    assert escalated == {APPROVER_2, APPROVER_3, CONTROLLER}  # the next level's approvers and the controller
    assert written == 2 * len(level_1) + len(escalated)
    mine = client.get("/api/v1/notifications", headers=h(APPROVER_1)).json()["items"]
    assert [n["kind"] for n in mine] == ["reminder"] * 3
    read = client.post(f"/api/v1/notifications/{mine[0]['notification_id']}:read", headers=h(APPROVER_1))
    assert read.status_code == 200
    assert read.json()["read_at"] is not None
    stranger = client.post(f"/api/v1/notifications/{mine[1]['notification_id']}:read", headers=h(APPROVER_2))
    assert stranger.status_code == 404  # someone else's notification
    moved(client, waiting, "approve", APPROVER_1)
    assert tasks.run_reminders(engine, as_of=since + timedelta(hours=200)) == 0  # nothing waits any more


def test_a_waiting_batch_is_reminded_too(client: TestClient, engine: Engine) -> None:
    set_up(engine)
    open_period(engine)
    assign(engine, PREPARER_2, "journal_approver_l1", "US01")  # prepares a member, so is never reminded
    batch = client.post(
        "/api/v1/journal-batches", json={"entity_id": "US01", "description": "Reminded"}, headers=h(PREPARER)
    ).json()
    for user in (PREPARER, PREPARER_2):
        member = create(client, user=user)
        url = f"/api/v1/journal-batches/{batch['batch_id']}/journals"
        batch = client.post(url, json={"journal_id": member["journal_id"]}, headers=h(user, batch["etag"])).json()
    submitted = client.post(f"/api/v1/journal-batches/{batch['batch_id']}:submit", headers=h(PREPARER, batch["etag"]))
    assert submitted.status_code == 200, submitted.text
    with engine.connect() as conn:
        since: datetime = conn.execute(
            text("SELECT level_since FROM journal_batches WHERE batch_id = :b"), {"b": batch["batch_id"]}
        ).scalar_one()
    assert tasks.run_reminders(engine, as_of=since + timedelta(hours=25)) == 3
    reminded = notes(engine, "reminder")
    assert {u for u, _ in reminded} == {APPROVER_1, APPROVER_2, APPROVER_3}
    assert all(key.startswith(f"reminder:batch:{batch['batch_id']}:1:1:1:") for _, key in reminded)
