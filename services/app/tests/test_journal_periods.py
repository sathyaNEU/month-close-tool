"""Accounting periods (Gate 7a): the moves and who may make them, what closed and locked periods refuse, and the
race between closing a period and submitting a journal into it, which the row locks prevent."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient
from journalkit import APPROVER_1, AUDITOR, CONTROLLER, PERIOD, PREPARER, create, h, moved, open_period, set_up, step
from sqlalchemy import Engine, text
from test_journal_posting import play_job, start_run

from close_app.context import AppContext
from close_app.errors import Problem
from close_app.identity import Principal
from close_app.journals import periods, store, workflow
from close_app.journals.setup import active
from close_platform.compute.fake import FakeComputeBackend
from close_platform.outbox import Relay
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg
BASE = "/api/v1/periods/US01"


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)


def move(client: TestClient, period: dict[str, Any], verb: str, user: str = CONTROLLER, **payload: Any) -> Any:
    return client.post(f"{BASE}/{period['period']}:{verb}", json=payload or None, headers=h(user, period["etag"]))


def test_the_moves_and_who_may_make_them(client: TestClient, ready: None) -> None:
    body = {"entity_id": "US01", "period": PERIOD}
    assert client.post("/api/v1/periods", json=body, headers=h(PREPARER)).status_code == 403
    opened = client.post("/api/v1/periods", json=body, headers=h(CONTROLLER))
    assert opened.status_code == 201
    p = opened.json()
    assert (p["status"], p["starts_on"], p["ends_on"]) == ("open", "2026-09-01", "2026-09-30")
    assert client.post("/api/v1/periods", json=body, headers=h(CONTROLLER)).status_code == 409  # already open
    assert client.post(f"{BASE}/{PERIOD}:close", headers=h(CONTROLLER)).status_code == 428  # no If-Match
    table = [  # (from, move, status code)
        ("open", "lock", 409),
        ("open", "reopen", 409),
        ("open", "close", 200),
        ("closed", "close", 409),
        ("closed", "reopen", 422),  # a reopen needs a reason
        ("closed", "lock", 200),
        ("locked", "reopen", 409),  # a locked period stays locked
        ("locked", "close", 409),
        ("locked", "lock", 409),
    ]
    for status, verb, expected in table:
        current = client.get(f"{BASE}/{PERIOD}", headers=h(AUDITOR)).json()
        assert current["status"] == status, (status, verb)
        r = move(client, current, verb)
        assert r.status_code == expected, (status, verb, r.text)
    assert move(client, current, "close", AUDITOR).status_code == 403  # reading is not managing
    reopenable = client.post("/api/v1/periods", json={"entity_id": "US01", "period": "2026-10"}, headers=h(CONTROLLER))
    closed = move(client, reopenable.json(), "close").json()
    reopened = move(client, closed, "reopen", reason="a late carrier invoice").json()
    assert (reopened["status"], reopened["reopen_reason"]) == ("open", "a late carrier invoice")
    listed = client.get("/api/v1/periods?entity_id=US01", headers=h(AUDITOR)).json()["items"]
    assert [x["period"] for x in listed] == ["2026-10", "2026-09"]


def test_closed_and_locked_periods_refuse_journals(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    ready: None,
) -> None:
    open_period(engine)
    draft = create(client)
    waiting = moved(client, create(client), "submit", PREPARER)
    period = client.get(f"{BASE}/{PERIOD}", headers=h(CONTROLLER)).json()
    refused = move(client, period, "close")
    assert refused.status_code == 409
    assert refused.json()["detail"] == f"US01-{waiting['number']}"
    moved(client, waiting, "approve", APPROVER_1)
    unposted = move(client, period, "close")  # Stage 7b: approved isn't enough, it must be in the ledger
    assert (unposted.status_code, unposted.json()["detail"]) == (409, f"US01-{waiting['number']}")
    relay = Relay(engine, ctx.coordinator.handlers(), instance_id="test-relay")
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    closed = move(client, period, "close").json()
    assert closed["status"] == "closed"
    assert step(client, draft, "submit", PREPARER).status_code == 409
    late = {
        "entity_id": "US01",
        "template_id": "sample_standard_journal",
        "accounting_date": "2026-09-20",
        "description": "Too late for September",
        "lines": [
            {"account": "5020", "side": "DR", "amount_minor": 1},
            {"account": "2410", "side": "CR", "amount_minor": 1},
        ],
    }
    assert client.post("/api/v1/journals", json=late, headers=h(PREPARER)).status_code == 409
    move(client, closed, "lock")
    assert moved(client, draft, "cancel", CONTROLLER)["status"] == "cancelled"  # the controller may cancel a draft


PREPARER_PRINCIPAL = Principal(PREPARER, "p@x", "p", frozenset({"US01"}), frozenset({("journal_preparer", "US01")}))


def _race(engine: Engine, journal_id: str) -> tuple[bool, BaseException | None]:
    """Close the period while a journal in it is submitted, the close pausing between its check and its update.
    Returns whether the submit had to wait for the close, and what the submit raised."""
    paused, release, submitted = threading.Event(), threading.Event(), threading.Event()
    outcome: dict[str, BaseException | None] = {"submit": None, "close": None}

    def pause() -> None:
        paused.set()
        release.wait(timeout=10)

    def close() -> None:
        try:
            with engine.begin() as tx:
                row = periods.load(tx, "US01", PERIOD, lock=True)
                assert row is not None
                periods.move(tx, row, "close", actor=CONTROLLER, reason=None, request_id=None)
        except BaseException as exc:
            outcome["close"] = exc

    def submit() -> None:
        try:
            with engine.begin() as tx:
                row = store.load(tx, journal_id, lock=True)
                assert row is not None
                workflow.submit(tx, active(tx), row, p=PREPARER_PRINCIPAL, request_id=None)
        except BaseException as exc:
            outcome["submit"] = exc
        finally:
            submitted.set()

    periods.pause_after_close_check = pause
    try:
        closer = threading.Thread(target=close)
        closer.start()
        assert paused.wait(timeout=10)
        submitter = threading.Thread(target=submit)
        submitter.start()
        waited = not submitted.wait(timeout=1.0)
        release.set()
        closer.join(timeout=10)
        submitter.join(timeout=10)
    finally:
        periods.pause_after_close_check = periods._no_pause
    assert outcome["close"] is None, outcome["close"]
    return waited, outcome["submit"]


def _pending_in_closed_period(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM journals j JOIN accounting_periods p "
                    "ON p.entity_id = j.entity_id AND p.period = j.period "
                    "WHERE j.status = 'pending_approval' AND p.status <> 'open'"
                )
            ).scalar_one()
        )


def test_the_period_lock_keeps_a_journal_out_of_a_closing_period(
    client: TestClient, engine: Engine, ready: None
) -> None:
    open_period(engine)
    journal = create(client)
    waited, raised = _race(engine, journal["journal_id"])
    assert waited  # the submit queued behind the close...
    assert isinstance(raised, Problem)
    assert raised.title == "The period isn't open"  # ...and then saw the period closed
    assert _pending_in_closed_period(engine) == 0


def test_without_the_locks_the_race_leaves_a_journal_waiting_in_a_closed_period(
    client: TestClient, engine: Engine, ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: with the lock clauses removed, the same interleaving breaks the rule."""
    monkeypatch.setattr(periods, "_ONE_LOCKED", periods._ONE)
    monkeypatch.setattr(
        workflow, "_PERIOD_SHARE", text("SELECT * FROM accounting_periods WHERE entity_id = :e AND period = :p")
    )
    open_period(engine)
    journal = create(client)
    waited, raised = _race(engine, journal["journal_id"])
    assert not waited
    assert raised is None
    assert _pending_in_closed_period(engine) == 1
