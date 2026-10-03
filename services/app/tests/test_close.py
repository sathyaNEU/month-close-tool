"""The period close in the control plane (Gate 8b).

- **The soft close** takes manual entries (journals, imports, approvals, posting) and refuses automated ones
  (recurring journals, automatic reversals), and waits for a journaling run like a close.
- **The checklist's configuration** is refused when a preparer or reviewer lacks the role, or names an entity
  outside the entity set. Its shape (ids, dependencies, cycles, distinct people) is the contracts' unit tests'.
- **Generating** freezes the tasks with business-day due dates from the entity's calendar, read through the narrow
  lookup, and is refused when the calendar can't give them.
- **Tasks** follow their dependencies, assignees and reviewers. A send-back, a reopen and a reassignment are kept in
  the history and audited, and an automatic task records its check and evidence.
- **Reminders** go once a day to whoever a due task waits on, and escalate to the controller.
- **The hard close** waits for every task, and for a GL check made after the ledger's last change. A period without
  a checklist closes as before.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from journalkit import (
    APPROVER_1,
    APPROVER_2,
    AUDITOR,
    CONTROLLER,
    PERIOD,
    PREPARER,
    activate,
    approved,
    create,
    h,
    moved,
    open_period,
    set_up,
    step,
)
from journalkit import body as journal_body
from sqlalchemy import Engine, text
from test_journal_fx import _alembic
from test_journal_posting import start_run as start_posting
from test_ledger import manual, rollup

from close_app.close import checklists
from close_app.context import SCHEMA_HEAD
from close_app.journals import tasks as journal_tasks
from close_contracts.config.close import CloseChecklist, CloseTask, TaskReminders
from close_contracts.config.envelope import ConfigKind
from close_contracts.samples.journal_workflow import SAMPLE_RECURRING
from close_platform.query.calendars import ReferenceCalendars
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg

CHECKLIST = CloseChecklist(
    checklist_id="test_close",
    name="Test close",
    tasks=(
        CloseTask(task_id="cutoff", name="Cut-off", due_day=-1, preparer=PREPARER, reviewer=APPROVER_1,
                  entities=("US01",)),
        CloseTask(task_id="settled", name="Journals settled", due_day=1, check="journals_settled"),
        CloseTask(task_id="accrual", name="Accrual", due_day=1, depends_on=("settled",), preparer=PREPARER,
                  reviewer=APPROVER_2, entities=("US01",)),
        CloseTask(task_id="balanced", name="Balanced", due_day=3, depends_on=("settled",), check="gl_balanced"),
    ),
    reminders=TaskReminders(escalate_after_days=2),
)  # fmt: skip
TASKS = "/api/v1/close/tasks/US01/" + PERIOD


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)
    open_period(engine)
    open_period(engine, period="2026-10")
    activate(engine, ConfigKind.CLOSE_CHECKLIST, CHECKLIST.checklist_id, CHECKLIST)


@pytest.fixture
def balanced(lake: ReferenceBackend) -> ReferenceBackend:
    lake.load("ledger.gl_rollup_lines", [rollup(PERIOD, "1110", "DR", 900), rollup(PERIOD, "4010", "CR", 900)])
    lake.load("ledger.journal_lines", [manual(PERIOD, "5020", "DR", 50, 1), manual(PERIOD, "2410", "CR", 50, 2)])
    return lake


def period_of(client: TestClient, period: str = PERIOD) -> dict[str, Any]:
    found: dict[str, Any] = client.get(f"/api/v1/periods/US01/{period}", headers=h(CONTROLLER)).json()
    return found


def move_period(client: TestClient, verb: str, period: str = PERIOD, **body: Any) -> Any:
    url = f"/api/v1/periods/US01/{period}:{verb}"
    return client.post(url, json=body or None, headers=h(CONTROLLER, period_of(client, period)["etag"]))


def generate(client: TestClient, period: str = PERIOD, entity: str = "US01") -> Any:
    return client.post("/api/v1/close/checklists", json={"entity_id": entity, "period": period}, headers=h(CONTROLLER))


def act(client: TestClient, task_id: str, verb: str, user: str, **body: Any) -> Any:
    return client.post(f"{TASKS}/{task_id}:{verb}", json=body or None, headers=h(user))


def done(client: TestClient, task_id: str, verb: str, user: str, **body: Any) -> dict[str, Any]:
    r = act(client, task_id, verb, user, **body)
    assert r.status_code == 200, (task_id, verb, r.text)
    out: dict[str, Any] = r.json()
    return out


# ---------------------------------------------------------------------------------------------- the soft close


def test_a_soft_closed_period_takes_manual_entries(client: TestClient, engine: Engine, ready: None) -> None:
    waiting = create(client)
    soft = move_period(client, "soft-close")
    assert (soft.status_code, soft.json()["status"], soft.json()["soft_closed_by"]) == (200, "soft_closed", CONTROLLER)
    adjustment = create(client, description="A late carrier invoice found in the close review")  # create...
    edited = client.put(
        f"/api/v1/journals/{adjustment['journal_id']}",
        json=journal_body(description="A late carrier invoice, accrued"),
        headers=h(PREPARER, adjustment["etag"]),
    )
    assert edited.status_code == 200, edited.text  # ...edit...
    assert approved(client, waiting)["status"] == "approved"  # ...and approve
    csv = (
        "journal_key,template_id,accounting_date,description,account,side,amount\n"
        f"J1,sample_standard_journal,{PERIOD}-29,Imported late accrual,5020,DR,1.00\nJ1,,,,2410,CR,1.00\n"
    )
    imported = client.post(
        "/api/v1/journal-imports?entity_id=US01",
        content=csv.encode(),
        headers=h(PREPARER) | {"Content-Type": "text/csv"},
    )
    assert imported.status_code == 201, imported.text
    posting = start_posting(client)  # the approved journal posts
    assert posting.status_code == 202, posting.text
    assert move_period(client, "reopen", reason="a late invoice").json()["status"] == "open"


def test_a_soft_closed_period_refuses_automated_entries(client: TestClient, engine: Engine, ready: None) -> None:
    monthly = SAMPLE_RECURRING[0].model_copy(update={"preparer": PREPARER})
    activate(engine, ConfigKind.RECURRING_JOURNAL, monthly.recurring_id, monthly)
    assert move_period(client, "soft-close").status_code == 200
    assert journal_tasks.run_recurring(engine, as_of=datetime(2026, 9, 29, 12, tzinfo=UTC)) == 0
    with engine.connect() as conn:
        skipped = conn.execute(
            text("SELECT message FROM notifications WHERE dedup_key = :k"),
            {"k": f"recurring_skipped:{monthly.recurring_id}:{PERIOD}"},
        ).scalar_one()
    assert skipped.endswith(f"period {PERIOD} is soft_closed")
    # An accrual in August would reverse into September, which takes no automated entries now.
    open_period(engine, period="2026-08")
    accrual = create(
        client,
        template_id="sample_accrual",
        header={"accrual_type": "carrier"},
        description="Accrued carrier charges",
        accounting_date="2026-08-20",
    )
    approved(client, accrual)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM journals WHERE source = 'reversal'")).scalar_one() == 0


def test_a_soft_close_waits_for_a_journaling_run(client: TestClient, engine: Engine, ready: None) -> None:
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO config_snapshots (snapshot_hash, purpose, configuration_hash, entity_id, content, "
                "created_by) VALUES (:h, 'journaling', :h, 'US01', '{}'::jsonb, 't')"
            ),
            {"h": "a" * 64},
        )
        tx.execute(
            text(
                "INSERT INTO journaling_runs (run_id, entity_id, requested_by, snapshot_hash, configuration_hash, "
                "status) VALUES (:id, 'US01', 't', :h, :h, 'running')"
            ),
            {"id": uuid.uuid4(), "h": "a" * 64},
        )
    refused = move_period(client, "soft-close")
    assert (refused.status_code, refused.json()["title"]) == (409, "A journaling run is in progress")
    assert move_period(client, "lock").status_code == 409  # only a closed period locks


# ---------------------------------------------------------------------------------------------- configuration


def test_a_checklist_needs_people_with_the_roles_and_entities_in_the_set(client: TestClient, engine: Engine) -> None:
    set_up(engine)
    for scope in ("GLOBAL",):
        assign(engine, "author", "config_author", scope)
        assign(engine, "approver", "config_approver", scope)
    stranger = CHECKLIST.model_copy(update={"checklist_id": "test_strangers"})
    tasks = list(stranger.tasks)
    tasks[0] = tasks[0].model_copy(update={"preparer": "nobody", "reviewer": AUDITOR})  # no close.prepare, no review
    spec = stranger.model_copy(update={"tasks": tuple(tasks)}).model_dump(mode="json")
    headers = {"X-Forwarded-User": "author", "X-Forwarded-Email": "a@x"}
    outside = spec | {"tasks": [spec["tasks"][0] | {"entities": ["XX01"]}, *spec["tasks"][1:]]}
    r = client.post(
        "/api/v1/close-checklists",
        json={"effective_from": "2026-06-01", "spec": outside},
        headers=headers | {"Idempotency-Key": "key-outside"},
    )
    assert (r.status_code, r.json()["issues"][0]["code"]) == (422, "unknown_entity")
    created = client.post(
        "/api/v1/close-checklists",
        json={"effective_from": "2026-06-01", "spec": spec},
        headers=headers | {"Idempotency-Key": "key-created"},
    )
    assert created.status_code == 201, created.text
    v = created.json()
    base = f"/api/v1/close-checklists/{v['config_id']}/versions/1"
    approver = {"X-Forwarded-User": "approver", "X-Forwarded-Email": "b@x"}
    submitted = client.post(
        f"{base}:submit", headers=headers | {"Idempotency-Key": "key-submit", "If-Match": v["etag"]}
    )
    assert submitted.status_code == 200, submitted.text
    v = submitted.json()
    v = client.post(
        f"{base}:approve", headers=approver | {"Idempotency-Key": "key-approve", "If-Match": v["etag"]}
    ).json()
    activated = client.post(
        f"{base}:activate", headers=approver | {"Idempotency-Key": "key-activate", "If-Match": v["etag"]}
    )
    assert activated.status_code == 409
    assert {(i["code"], i["path"]) for i in activated.json()["issues"]} == {
        ("preparer_lacks_role", "/tasks/0/preparer"),
        ("reviewer_lacks_role", "/tasks/0/reviewer"),
    }


# ---------------------------------------------------------------------------------------------- generating


def test_a_checklist_is_generated_with_business_day_due_dates_and_frozen(
    client: TestClient, engine: Engine, ready: None, calendars: ReferenceCalendars
) -> None:
    made = generate(client)
    assert made.status_code == 201, made.text
    out = made.json()
    assert (out["checklist_id"], out["checklist_version"], out["calendar_code"]) == ("test_close", 1, "US_FED")
    # September 2026 ends on a Wednesday: BD-1 is the 30th, BD+1 October 1st, BD+3 Monday the 5th.
    assert {t["task_id"]: (t["due_day"], t["due_on"], t["position"]) for t in out["tasks"]} == {
        "cutoff": (-1, "2026-09-30", 1),
        "settled": (1, "2026-10-01", 2),
        "accrual": (1, "2026-10-01", 3),
        "balanced": (3, "2026-10-05", 4),
    }
    assert calendars.calls[-1][0] == "US01"  # through the narrow lookup, outside the transaction
    # August ends on Monday the 31st, and Labor Day (September 7th) isn't a business day: BD+5 is the 8th.
    late = CHECKLIST.model_copy(update={"tasks": (CHECKLIST.tasks[1].model_copy(update={"due_day": 5}),)})
    with engine.begin() as tx:
        tx.execute(text("UPDATE config_versions SET status = 'retired' WHERE kind = 'close_checklist'"))
    activate(engine, ConfigKind.CLOSE_CHECKLIST, late.checklist_id, late, version=2)
    open_period(engine, period="2026-08")
    august = generate(client, "2026-08").json()
    assert [(t["task_id"], t["due_on"]) for t in august["tasks"]] == [("settled", "2026-09-08")]
    # The September checklist kept its version 1 tasks.
    again = client.get(f"/api/v1/close/checklists?entity_id=US01&period={PERIOD}", headers=h(AUDITOR)).json()
    assert (again["checklist_version"], len(again["tasks"])) == (1, 4)
    assert generate(client).status_code == 409  # once per period


def test_generating_is_refused_without_a_calendar_or_a_checklist(
    client: TestClient, engine: Engine, ready: None
) -> None:
    for entity in ("EU01", "UK01"):
        open_period(engine, entity_id=entity)
        assign(engine, CONTROLLER, "controller", entity)
    no_days = generate(client, entity="EU01")  # TARGET2 has no days in the lookup
    assert (no_days.status_code, no_days.json()["title"]) == (422, "The calendar doesn't cover the due dates")
    no_code = generate(client, entity="UK01")  # no calendar code for UK01
    assert (no_code.status_code, no_code.json()["title"]) == (422, "The entity has no business calendar")
    assert move_period(client, "close", "2026-10").status_code == 200
    closed = generate(client, "2026-10")
    assert (closed.status_code, closed.json()["title"]) == (409, "The period is closed")
    with engine.begin() as tx:
        tx.execute(text("UPDATE config_versions SET status = 'retired' WHERE kind = 'close_checklist'"))
    retired = generate(client)
    assert (retired.status_code, retired.json()["title"]) == (409, "No close checklist is active")
    assert (
        client.post(
            "/api/v1/close/checklists", json={"entity_id": "US01", "period": PERIOD}, headers=h(PREPARER)
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------------------------- tasks


def test_tasks_follow_their_dependencies_assignees_and_reviewers(
    client: TestClient, engine: Engine, ready: None
) -> None:
    assert generate(client).status_code == 201
    assert act(client, "cutoff", "prepare", CONTROLLER).json()["title"] == "The task's preparer is someone else"
    assert act(client, "cutoff", "prepare", APPROVER_1).status_code == 403  # no close.prepare at all
    waiting = act(client, "accrual", "prepare", PREPARER)
    assert (waiting.status_code, waiting.json()["detail"]) == (409, "settled")  # its dependency isn't done
    assert act(client, "settled", "prepare", PREPARER).json()["title"] == "An automatic task is completed by its check"
    prepared = done(client, "cutoff", "prepare", PREPARER, note="labels cut off at 23:59")
    assert (prepared["status"], prepared["prepared_by"], prepared["note"]) == (
        "prepared",
        PREPARER,
        "labels cut off at 23:59",
    )
    assert act(client, "cutoff", "review", APPROVER_2).json()["title"] == "The task's reviewer is someone else"
    sent = done(client, "cutoff", "send-back", APPROVER_1, reason="attach the carrier extract")
    assert (sent["status"], sent["prepared_by"]) == ("open", None)
    done(client, "cutoff", "prepare", PREPARER, note="extract attached")
    reviewed = done(client, "cutoff", "review", APPROVER_1)
    assert (reviewed["status"], reviewed["reviewed_by"]) == ("done", APPROVER_1)
    # The controller reopens and reassigns, and a reviewer never reviews their own preparation.
    reopened = done(client, "cutoff", "reopen", CONTROLLER, reason="the extract was incomplete")
    assert reopened["status"] == "open"
    done(client, "cutoff", "reassign", CONTROLLER, preparer=CONTROLLER)
    done(client, "cutoff", "prepare", CONTROLLER)
    assert act(client, "cutoff", "reassign", CONTROLLER, reviewer=CONTROLLER).status_code == 409  # the preparer
    swapped = done(client, "cutoff", "reassign", CONTROLLER, preparer=PREPARER, reviewer=CONTROLLER)
    assert (swapped["preparer"], swapped["reviewer"], swapped["prepared_by"]) == (PREPARER, CONTROLLER, CONTROLLER)
    own = act(client, "cutoff", "review", CONTROLLER)
    assert (own.status_code, own.json()["title"]) == (409, "A reviewer can't review their own preparation")
    assert act(client, "cutoff", "reassign", CONTROLLER, reviewer="nobody").status_code == 422  # lacks close.review
    assert act(client, "cutoff", "reassign", CONTROLLER, preparer=APPROVER_2).status_code == 422  # lacks close.prepare
    assert act(client, "cutoff", "prepare", AUDITOR).status_code == 403  # no close.prepare
    task = client.get(f"{TASKS}/cutoff", headers=h(AUDITOR)).json()
    assert [e["action"] for e in task["history"]] == [
        "generate", "prepare", "reject", "prepare", "review", "reopen", "reassign", "prepare", "reassign",
    ]  # fmt: skip
    with engine.connect() as conn:
        actions = (
            conn.execute(
                text("SELECT action FROM audit_events WHERE target = :t AND action LIKE 'close.task.%' ORDER BY seq"),
                {"t": checklists.target("US01", PERIOD, "cutoff")},
            )
            .scalars()
            .all()
        )
    assert actions == [
        "close.task.prepare", "close.task.reject", "close.task.prepare", "close.task.review", "close.task.reopen",
        "close.task.reassign", "close.task.prepare", "close.task.reassign",
    ]  # fmt: skip


def test_an_automatic_task_records_its_check_and_evidence(client: TestClient, engine: Engine, ready: None) -> None:
    assert generate(client).status_code == 201
    journal = moved(client, create(client), "submit", PREPARER)  # waiting for approval
    failed = done(client, "settled", "check", PREPARER)
    assert (failed["passed"], failed["status"], failed["evidence"]) == (
        False,
        "open",
        {"waiting": [journal["number"]], "passed": False},
    )
    assert step(client, journal, "recall", PREPARER).status_code == 200
    passed = done(client, "settled", "check", PREPARER)
    assert (passed["passed"], passed["status"], passed["prepared_by"]) == (True, "done", PREPARER)
    assert passed["checked_at"] is not None
    assert (
        act(client, "accrual", "check", PREPARER).json()["title"]
        == "A manual task is prepared by its preparer, not checked"
    )


# ---------------------------------------------------------------------------------------------- reminders


def _notices(engine: Engine) -> set[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT user_id, dedup_key FROM notifications WHERE task_ref IS NOT NULL")).all()
    return {(str(u), str(k)) for u, k in rows}


def test_reminders_go_once_a_day_to_whoever_a_task_waits_on(client: TestClient, engine: Engine, ready: None) -> None:
    assert generate(client).status_code == 201
    ref = f"US01:{PERIOD}"
    assert checklists.run_reminders(engine, as_of=datetime(2026, 9, 29, 12, tzinfo=UTC)) == 0
    assert checklists.run_reminders(engine, as_of=datetime(2026, 9, 30, 12, tzinfo=UTC)) == 1
    assert checklists.run_reminders(engine, as_of=datetime(2026, 9, 30, 18, tzinfo=UTC)) == 0  # once a day
    assert checklists.run_reminders(engine, as_of=datetime(2026, 10, 1, 12, tzinfo=UTC)) == 2
    done(client, "cutoff", "prepare", PREPARER)  # now it waits on its reviewer
    assert checklists.run_reminders(engine, as_of=datetime(2026, 10, 2, 12, tzinfo=UTC)) == 3
    assert _notices(engine) == {
        (PREPARER, f"task_due:{ref}:cutoff:2026-09-30"),
        (PREPARER, f"task_due:{ref}:cutoff:2026-10-01"),
        (PREPARER, f"task_due:{ref}:accrual:2026-10-01"),
        (APPROVER_1, f"task_due:{ref}:cutoff:2026-10-02"),
        (CONTROLLER, f"task_escalation:{ref}:cutoff:{CONTROLLER}:2026-10-02"),  # two days overdue
        (PREPARER, f"task_due:{ref}:accrual:2026-10-02"),
    }
    listed = client.get("/api/v1/notifications", headers=h(CONTROLLER)).json()["items"]
    assert [n["kind"] for n in listed] == ["task_escalation"]


# ---------------------------------------------------------------------------------------------- the hard close


def _finish(client: TestClient) -> None:
    done(client, "cutoff", "prepare", PREPARER)
    done(client, "cutoff", "review", APPROVER_1)
    done(client, "settled", "check", PREPARER)
    done(client, "accrual", "prepare", PREPARER)
    done(client, "accrual", "review", APPROVER_2)
    assert done(client, "balanced", "check", PREPARER)["passed"]


def test_a_period_with_a_checklist_closes_once_every_task_is_done(
    client: TestClient, engine: Engine, ready: None, balanced: ReferenceBackend
) -> None:
    assert generate(client).status_code == 201
    early = move_period(client, "close")
    assert (early.status_code, early.json()["title"]) == (409, "Soft-close the period first")
    assert move_period(client, "soft-close").status_code == 200
    waiting = move_period(client, "close")
    assert (waiting.status_code, waiting.json()["title"]) == (409, "The close checklist isn't done")
    assert waiting.json()["detail"] == "cutoff is open; settled is open; accrual is open; balanced is open"
    _finish(client)  # the tasks move while the period is soft-closed
    closed = move_period(client, "close")
    assert (closed.status_code, closed.json()["status"]) == (200, "closed")
    assert act(client, "cutoff", "reopen", CONTROLLER, reason="too late").status_code == 409  # the period is closed
    # A period without a checklist closes as before, straight from open.
    assert move_period(client, "close", "2026-10").json()["status"] == "closed"


def test_a_gl_check_made_before_the_ledger_changed_is_made_again(
    client: TestClient, engine: Engine, ready: None, balanced: ReferenceBackend
) -> None:
    assert generate(client).status_code == 201
    assert move_period(client, "soft-close").status_code == 200
    _finish(client)
    with engine.begin() as tx:  # an ERP extract accepted after the check read the GL
        tx.execute(
            text(
                "INSERT INTO erp_trial_balances (extract_id, entity_id, period, version, status, sha256, currency, "
                "line_count, uploaded_by) VALUES (:id, 'US01', :p, 1, 'accepted', :h, 'USD', 0, 'erp')"
            ),
            {"id": uuid.uuid4(), "p": PERIOD, "h": "e" * 64},
        )
    stale = move_period(client, "close")
    assert stale.status_code == 409
    assert stale.json()["detail"] == "balanced was checked before the ledger last changed: check it again"
    assert done(client, "balanced", "check", PREPARER)["passed"]
    assert move_period(client, "close").json()["status"] == "closed"


def test_the_downgrade_refuses_while_a_period_is_soft_closed(
    client: TestClient, admin_engine: Engine, ready: None
) -> None:
    """0007 has no soft-closed status, so 0008's downgrade won't run while a period has it."""
    assert move_period(client, "soft-close").status_code == 200
    try:
        refused = _alembic("downgrade", "0007")
        assert refused.returncode != 0
        assert "soft-closed periods exist" in refused.stderr
    finally:
        _alembic("upgrade", "head")
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM close.alembic_version")).scalar_one() == SCHEMA_HEAD
