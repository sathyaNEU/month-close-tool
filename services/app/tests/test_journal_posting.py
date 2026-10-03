"""Posting and the ERP write-back (Gate 7b), with the fake Jobs backend standing in for the posting job: a run
freezes the approved journals, the job's result settles them, a lost result is recovered by a retry without writing
twice, the requester's own journals are skipped, batches post whole, and the write-back confirms document numbers.
Every posting and write-back move is tested from every state, and the ledger reads never cross entities."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from journalkit import (
    APPROVER_1,
    APPROVER_2,
    APPROVER_3,
    AUDITOR,
    CONTROLLER,
    PERIOD,
    PREPARER,
    approved,
    body,
    create,
    get,
    h,
    moved,
    open_period,
    set_up,
)
from pgtestkit import engine_for
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError
from test_journal_batches import acted, add, new_batch

from close_app.context import AppContext
from close_app.errors import Problem
from close_app.identity import Principal
from close_app.journals import periods, posting, workflow
from close_contracts.enums import JobStatus
from close_engine.jobs.post_journals import ledger_rows, posting_rows
from close_platform.compute.fake import FakeComputeBackend
from close_platform.outbox import Relay
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg
ERP = "7101"


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)
    open_period(engine)
    open_period(engine, period="2026-10")
    assign(engine, ERP, "integration_client", "US01")


@pytest.fixture
def relay(ctx: AppContext) -> Relay:
    return Relay(ctx.engine, ctx.coordinator.handlers(), instance_id="test-relay")


def start_run(client: TestClient, user: str = CONTROLLER) -> Any:
    return client.post("/api/v1/posting/runs", json={"entity_id": "US01"}, headers=h(user))


def queue(engine: Engine, run_id: str) -> list[dict[str, Any]]:
    """What the posting job reads through posting_queue (here as the app role, which may also read the view)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM posting_queue WHERE posting_run_id = :r ORDER BY number, line_no"), {"r": run_id}
        ).mappings()
        return [dict(r) for r in rows]


def play_job(
    engine: Engine,
    relay: Relay,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    run_id: str,
    *,
    status: JobStatus = JobStatus.SUCCEEDED,
    write: bool = True,
    report: bool = True,
    present: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Do what the posting job does: write the run's lines to the lake (unless already there), then finish with
    its result line (or with none, when the result was lost)."""
    assert relay.run_once() >= 1
    ext = fake.by_token[next(t for t in fake.by_token if uuid.UUID(run_id).hex in t)]
    rows = queue(engine, run_id)
    lines = ledger_rows(rows, uuid.UUID(run_id).hex, datetime(2026, 9, 30, tzinfo=UTC))
    journals = sorted({ln["journal_id"] for ln in lines})
    if write:  # each load is the table's next full version, so add to the last one
        fresh = [ln for ln in lines if ln["journal_id"] not in present]
        feed = posting_rows(fresh, uuid.UUID(run_id).hex, datetime(2026, 9, 30, tzinfo=UTC))
        for key, rows_ in (("ledger.journal_lines", fresh), ("ledger.journal_postings", feed)):
            lake.load(key, [*(lake.snapshots.get(key) or [[]])[-1], *rows_])
    result: dict[str, Any] | None = None
    if report:
        result = {
            "job": "post_journals",
            "fenced": False,
            "journals": len(journals),
            "written": [j for j in journals if j not in present],
            "already_present": [j for j in journals if j in present],
            "lines": len(lines),
            "commit_version": 7,
        }
    fake.advance(ext, status, result=result)
    assert ctx.coordinator.poll() == 1
    posting.reconcile(engine)
    return result


def statuses(client: TestClient) -> dict[int, str]:
    items = client.get("/api/v1/journals?entity_id=US01", headers=h(PREPARER)).json()["items"]
    return {j["number"]: j["status"] for j in items}


def test_a_run_posts_the_approved_journals_and_the_ledger_ties_out(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
) -> None:
    first, second = (
        approved(client, create(client)),
        approved(client, create(client, lines=(("5020", "DR", 700), ("2410", "CR", 700)))),
    )
    draft = create(client)
    started = start_run(client)
    assert started.status_code == 202, started.text
    run = started.json()
    assert (run["journal_count"], run["status"], run["skipped"]) == (2, "posting", [])
    assert statuses(client) == {first["number"]: "posting", second["number"]: "posting", draft["number"]: "draft"}
    play_job(engine, relay, ctx, fake, lake, run["run_id"])
    assert statuses(client)[first["number"]] == "posted"
    settled = client.get(f"/api/v1/posting/runs/{run['run_id']}", headers=h(AUDITOR)).json()
    assert (settled["status"], settled["written"], settled["already_present"], settled["commit_version"]) == (
        "reconciled",
        2,
        0,
        7,
    )
    tb = client.get(f"/api/v1/gl/trial-balance?entity_id=US01&period={PERIOD}", headers=h(AUDITOR)).json()
    assert tb["balanced"]
    assert tb["debits"] == 12_345 + 700
    assert {a["account"]: (a["debits"], a["credits"]) for a in tb["accounts"]} == {
        "2410": (0, 13_045),
        "5020": (13_045, 0),
    }
    lines = client.get("/api/v1/gl/lines?entity_id=US01&account=5020", headers=h(AUDITOR)).json()["lines"]
    assert {ln["journal_id"] for ln in lines} == {first["journal_id"], second["journal_id"]}
    tie = client.get("/api/v1/posting/tie-out?entity_id=US01", headers=h(AUDITOR)).json()
    assert tie["ties"]
    assert tie["items"][0]["ledger"] == {"lines": 4, "debits": 13_045, "credits": 13_045}


def test_the_requester_never_posts_their_own_journals(client: TestClient, engine: Engine, ready: None) -> None:
    assign(engine, CONTROLLER, "journal_preparer", "US01")
    own = approved(client, create(client, user=CONTROLLER))
    refused = start_run(client)
    assert (refused.status_code, refused.json()["title"]) == (409, "Nothing to post")
    assert refused.json()["skipped"][0]["reason"] == "you prepared it"
    other = approved(client, create(client))
    run = start_run(client).json()
    assert run["journal_count"] == 1
    assert [s["number"] for s in run["skipped"]] == [own["number"]]
    assert start_run(client).status_code == 409  # the entity's ledger lease is held by the first run
    assert statuses(client)[other["number"]] == "posting"


def test_a_failed_run_returns_its_journals_and_a_retry_finds_what_a_lost_result_hid(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
) -> None:
    j = approved(client, create(client))
    failed = start_run(client).json()
    play_job(engine, relay, ctx, fake, lake, failed["run_id"], status=JobStatus.FAILED, write=False, report=False)
    assert statuses(client)[j["number"]] == "approved"
    assert client.get(f"/api/v1/posting/runs/{failed['run_id']}", headers=h(AUDITOR)).json()["status"] == "failed"
    # The next job commits its lines, then dies before its result line: the run fails, the lines stay.
    lost = start_run(client).json()
    play_job(engine, relay, ctx, fake, lake, lost["run_id"], report=False)
    assert statuses(client)[j["number"]] == "approved"
    # A retry finds the lines already there, writes none, and the journal is posted.
    retry = start_run(client).json()
    play_job(engine, relay, ctx, fake, lake, retry["run_id"], present=frozenset({j["journal_id"]}))
    assert statuses(client)[j["number"]] == "posted"
    settled = client.get(f"/api/v1/posting/runs/{retry['run_id']}", headers=h(AUDITOR)).json()
    assert (settled["written"], settled["already_present"]) == (0, 1)
    lines = client.get("/api/v1/gl/lines?entity_id=US01", headers=h(AUDITOR)).json()["lines"]
    assert len(lines) == 2  # written once
    kinds = [
        e["kind"] for e in client.get("/api/v1/journal-exceptions?entity_id=US01", headers=h(AUDITOR)).json()["items"]
    ]
    assert kinds.count("post_failed") == 2


def test_the_erp_write_back(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
) -> None:
    j = approved(client, create(client))
    waiting = approved(client, create(client))
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    feed = client.get("/api/v1/gl/postings?entity_id=US01", headers=h(ERP)).json()["postings"]
    assert {f["journal_id"] for f in feed} == {j["journal_id"], waiting["journal_id"]}
    body = {"entries": [{"journal_id": j["journal_id"], "document_id": "ERP-US01-000001"}]}
    confirmed = client.post("/api/v1/posting/status", json=body, headers=h(ERP))
    assert confirmed.status_code == 200, confirmed.text
    after = client.get(f"/api/v1/journals/{j['journal_id']}", headers=h(AUDITOR)).json()
    assert (after["status"], after["erp_document_id"]) == ("erp_confirmed", "ERP-US01-000001")
    assert client.post("/api/v1/posting/status", json=body, headers=h(ERP)).status_code == 200  # the same again
    other = {"entries": [{"journal_id": j["journal_id"], "document_id": "ERP-US01-999999"}]}
    clash = client.post("/api/v1/posting/status", json=other, headers=h(ERP))
    assert (clash.status_code, clash.json()["issues"][0]["code"]) == (409, "different_document")
    draft = create(client)
    early = {"entries": [{"journal_id": draft["journal_id"], "document_id": "ERP-X"}]}
    assert client.post("/api/v1/posting/status", json=early, headers=h(ERP)).json()["issues"][0]["code"] == "not_posted"
    assign(engine, "7102", "integration_client", "EU01")  # a client in another entity can't see the journal
    elsewhere = client.post("/api/v1/posting/status", json=body, headers=h("7102"))
    assert (elsewhere.status_code, elsewhere.json()["issues"][0]["code"]) == (404, "unknown_journal")
    assert client.post("/api/v1/posting/status", json=body, headers=h(PREPARER)).status_code == 403
    assert client.get("/api/v1/gl/postings?entity_id=US01", headers=h(PREPARER)).status_code == 403


def test_periods_wait_for_posting_and_posted_journals_reverse(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
) -> None:
    j = approved(client, create(client))
    period = client.get(f"/api/v1/periods/US01/{PERIOD}", headers=h(CONTROLLER)).json()
    refused = client.post(f"/api/v1/periods/US01/{PERIOD}:close", headers=h(CONTROLLER, period["etag"]))
    assert (refused.status_code, refused.json()["detail"]) == (409, f"US01-{j['number']}")  # approved, unposted
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    closed = client.post(f"/api/v1/periods/US01/{PERIOD}:close", headers=h(CONTROLLER, period["etag"]))
    assert closed.status_code == 200
    posted = client.get(f"/api/v1/journals/{j['journal_id']}", headers=h(PREPARER)).json()
    back = moved(client, posted, "reverse", PREPARER)
    assert (back["reverses_journal_id"], back["accounting_date"]) == (j["journal_id"], "2026-10-01")


def test_the_ledger_reader_role_sees_only_the_posting_queue(engine: Engine, ready: None) -> None:
    reader = engine_for("pg-ledger-reader.env")
    try:
        with reader.connect() as conn:
            conn.execute(text("SELECT count(*) FROM posting_queue")).scalar_one()
        for table in ("journals", "journal_lines", "role_assignments", "audit_events"):
            with pytest.raises(ProgrammingError, match="permission denied"), reader.connect() as conn:
                conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        with pytest.raises(ProgrammingError, match="permission denied"), reader.begin() as tx:
            tx.execute(text("UPDATE journals SET status = 'posted'"))
    finally:
        reader.dispose()


# ---------------------------------------------------------------------------------------------- states and batches


def _refresh(client: TestClient, journal: dict[str, Any]) -> dict[str, Any]:
    return get(client, journal, AUDITOR)


def _verb(client: TestClient, journal: dict[str, Any], verb: str) -> int:
    """One 7a move on a journal, by someone allowed to make it, so only the journal's state can refuse it."""
    j = _refresh(client, journal)
    url = f"/api/v1/journals/{j['journal_id']}"
    if verb == "edit":
        return client.put(url, json=body(), headers=h(PREPARER, j["etag"])).status_code
    user = APPROVER_1 if verb in {"approve", "reject"} else PREPARER
    payload = {"reason": "state table"} if verb == "reject" else None
    return client.post(f"{url}:{verb}", json=payload, headers=h(user, j["etag"])).status_code


def _confirm(client: TestClient, journal: dict[str, Any], document_id: str = "ERP-1") -> Any:
    entries = {"entries": [{"journal_id": journal["journal_id"], "document_id": document_id}]}
    return client.post("/api/v1/posting/status", json=entries, headers=h(ERP))


def test_every_posting_move_from_every_state(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
) -> None:
    posted, confirmed = approved(client, create(client)), approved(client, create(client))
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    assert _confirm(client, confirmed, "ERP-B").status_code == 200
    draft = create(client)
    pending = moved(client, create(client), "submit", PREPARER)
    cancelled = moved(client, create(client), "cancel", PREPARER)
    in_run = approved(client, create(client))
    run = start_run(client).json()  # a run takes only the approved journal, whatever else the entity holds
    assert (run["journal_count"], statuses(client)[in_run["number"]]) == (1, "posting")
    waiting = approved(client, create(client))  # approved after the run started: it waits for the next one
    journals = {
        "draft": draft,
        "pending_approval": pending,
        "cancelled": cancelled,
        "approved": waiting,
        "posting": in_run,
        "posted": posted,
        "erp_confirmed": confirmed,
    }
    assert {state: statuses(client)[j["number"]] for state, j in journals.items()} == {s: s for s in journals}
    # The write-back confirms only a posted journal; for a confirmed one only its own number again.
    write_back = {state: _confirm(client, j, "ERP-B") for state, j in journals.items() if state != "posted"}
    assert {state: r.status_code for state, r in write_back.items()} == {
        "draft": 409,
        "pending_approval": 409,
        "cancelled": 409,
        "approved": 409,
        "posting": 409,
        "erp_confirmed": 200,
    }
    assert _confirm(client, confirmed, "ERP-OTHER").json()["issues"][0]["code"] == "different_document"
    # The 7a moves on the states 7b adds: all refused, except reversing what is in the ledger.
    verbs = ("edit", "submit", "approve", "reject", "recall", "cancel", "reverse")
    table = {
        state: {v: _verb(client, journals[state], v) for v in verbs} for state in ("posting", "posted", "erp_confirmed")
    }
    refused = dict.fromkeys(verbs, 409)
    assert table == {
        "posting": refused,
        "posted": refused | {"reverse": 201},
        "erp_confirmed": refused | {"reverse": 201},
    }
    assert _confirm(client, posted, "ERP-A").status_code == 200
    assert statuses(client)[posted["number"]] == "erp_confirmed"


def approved_batch(client: TestClient, *journals: dict[str, Any]) -> dict[str, Any]:
    batch = new_batch(client)
    for j in journals:  # each preparer adds their own journal
        batch = add(client, batch, j, user=j["created_by"]).json()
    batch = acted(client, batch, "submit", PREPARER)
    for user in (APPROVER_1, APPROVER_2, APPROVER_3):
        if batch["status"] == "pending_approval":
            batch = acted(client, batch, "approve", user)
    assert batch["status"] == "approved", batch
    return batch


def test_batches_post_whole(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assign(engine, CONTROLLER, "journal_preparer", "US01")
    first, second = approved(client, create(client)), approved(client, create(client))
    whole = approved_batch(client, create(client), create(client))
    theirs = approved_batch(client, create(client), create(client, user=CONTROLLER))
    last = approved(client, create(client))
    monkeypatch.setattr(posting, "MAX_JOURNALS", 3)
    run = start_run(client).json()
    batch_numbers = sorted(j["number"] for j in whole["journals"])
    assert run["journal_count"] == 3  # the two singles, then the batch doesn't fit, so the last single does
    assert {s["number"]: s["reason"] for s in run["skipped"]} == {
        **dict.fromkeys(batch_numbers, "the run is full"),
        **dict.fromkeys((j["number"] for j in theirs["journals"]), "you prepared a journal in its batch"),
    }
    now = statuses(client)
    assert [now[n] for n in (first["number"], second["number"], last["number"], *batch_numbers)] == [
        "posting", "posting", "posting", "approved", "approved",
    ]  # fmt: skip
    play_job(engine, relay, ctx, fake, lake, run["run_id"])
    monkeypatch.setattr(posting, "MAX_JOURNALS", 500)
    batch_run = start_run(client).json()
    assert batch_run["journal_count"] == 2
    play_job(engine, relay, ctx, fake, lake, batch_run["run_id"], status=JobStatus.FAILED, write=False, report=False)
    assert [statuses(client)[n] for n in batch_numbers] == ["approved", "approved"]  # a failed run returns both
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    assert [statuses(client)[n] for n in batch_numbers] == ["posted", "posted"]


# ---------------------------------------------------------------------------------------------- periods and races

CONTROLLER_PRINCIPAL = Principal(CONTROLLER, "c@x", "c", frozenset({"US01"}), frozenset({("controller", "US01")}))


@pytest.mark.parametrize("locks", [True, False], ids=["with the locks", "without them"])
def test_a_close_racing_a_posting_start_is_refused_either_way(
    client: TestClient, engine: Engine, ctx: AppContext, ready: None, monkeypatch: pytest.MonkeyPatch, locks: bool
) -> None:
    """A close is refused while a period holds approved or posting journals, and a posting start only takes
    approved ones, so whichever commits first the close is refused. The state check protects this; the locks only
    make the close wait for the posting start. (A journal reaches ``approved`` only through a submit, whose race
    with a close the Stage 7a test covers.)"""
    if not locks:
        monkeypatch.setattr(periods, "_ONE_LOCKED", periods._ONE)
        monkeypatch.setattr(
            workflow, "_PERIOD_SHARE", text("SELECT * FROM accounting_periods WHERE entity_id = :e AND period = :p")
        )
    j = approved(client, create(client))
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    outcome: dict[str, BaseException | None] = {"start": None, "close": None}

    def start() -> None:
        try:
            with engine.begin() as tx:
                posting.start(tx, ctx.coordinator, "US01", p=CONTROLLER_PRINCIPAL, request_id=None)
                started.set()
                release.wait(timeout=10)
        except BaseException as exc:
            outcome["start"] = exc
            started.set()

    def close() -> None:
        try:
            with engine.begin() as tx:
                row = periods.load(tx, "US01", PERIOD, lock=True)
                assert row is not None
                periods.move(tx, row, "close", actor=CONTROLLER, reason=None, request_id=None)
        except BaseException as exc:
            outcome["close"] = exc
        finally:
            closed.set()

    starter = threading.Thread(target=start)
    starter.start()
    assert started.wait(timeout=10)
    closer = threading.Thread(target=close)
    closer.start()
    waited = not closed.wait(timeout=1.0)
    release.set()
    starter.join(timeout=10)
    closer.join(timeout=10)
    assert outcome["start"] is None, outcome["start"]
    assert waited is locks  # with the locks the close queues behind the posting start
    assert isinstance(outcome["close"], Problem)
    assert (outcome["close"].status, outcome["close"].detail) == (409, f"US01-{j['number']}")
    assert statuses(client)[j["number"]] == "posting"
    assert client.get(f"/api/v1/periods/US01/{PERIOD}", headers=h(CONTROLLER)).json()["status"] == "open"


def test_a_posted_accrual_still_reverses_when_the_next_period_opens(
    client: TestClient, engine: Engine, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend, relay: Relay
) -> None:
    set_up(engine)
    open_period(engine)  # October isn't open, so the reversal waits for it
    accrual = create(
        client, template_id="sample_accrual", header={"accrual_type": "carrier"}, description="Accrued carrier charges"
    )
    approved(client, accrual)
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    assert statuses(client)[accrual["number"]] == "posted"
    opened = client.post("/api/v1/periods", json={"entity_id": "US01", "period": "2026-10"}, headers=h(CONTROLLER))
    assert opened.status_code == 201, opened.text
    items = client.get("/api/v1/journals?entity_id=US01", headers=h(PREPARER)).json()["items"]
    reversal = next(j for j in items if j["source"] == "reversal")
    assert (reversal["reverses_journal_id"], reversal["accounting_date"]) == (accrual["journal_id"], "2026-10-01")


def test_a_posted_journal_still_counts_as_using_its_accounts(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
) -> None:
    lines = (("9999", "DR", 512_345), ("2410", "CR", 512_345))
    first = approved(client, create(client, lines=lines))
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    assert statuses(client)[first["number"]] == "posted"
    again = moved(client, create(client, lines=lines), "submit", PREPARER)
    assert again["risk_factors"]["seldom_used_account"] | {"weight": 0} == {
        "hit": False,
        "accounts": [],
        "window_days": again["risk_factors"]["seldom_used_account"]["window_days"],
        "weight": 0,
    }


# ---------------------------------------------------------------------------------------------- the ledger reads


def test_the_ledger_reads_never_cross_entities(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
) -> None:
    approved(client, create(client))
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    ours = lake.snapshots["ledger.journal_lines"][-1]
    theirs = [{**ln, "entity_id": "EU01", "journal_id": str(uuid.uuid4()), "amount_minor": 99} for ln in ours]
    lake.load("ledger.journal_lines", [*ours, *theirs])  # another entity's posting, in the same table version
    tb = client.get(f"/api/v1/gl/trial-balance?entity_id=US01&period={PERIOD}", headers=h(AUDITOR)).json()
    assert (tb["debits"], tb["credits"]) == (12_345, 12_345)
    lines = client.get("/api/v1/gl/lines?entity_id=US01", headers=h(AUDITOR)).json()["lines"]
    assert {ln["entity_id"] for ln in lines} == {"US01"}
    assert client.get("/api/v1/gl/trial-balance?entity_id=EU01", headers=h(AUDITOR)).status_code == 404  # invisible
    assign(engine, "7106", "auditor", "EU01")
    eu = client.get("/api/v1/gl/lines?entity_id=EU01", headers=h("7106")).json()["lines"]
    assert {ln["journal_id"] for ln in eu} == {ln["journal_id"] for ln in theirs}
    assert client.get("/api/v1/gl/lines?entity_id=US01", headers=h("7106")).status_code == 404
    assert all(plan.entities <= {"US01"} or plan.entities <= {"EU01"} for plan in lake.plans)


def test_a_difference_between_postgres_and_the_ledger_is_reported(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    relay: Relay,
    ready: None,
) -> None:
    approved(client, create(client))
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    assert client.get("/api/v1/posting/tie-out?entity_id=US01", headers=h(AUDITOR)).json()["ties"]
    lake.load("ledger.journal_lines", lake.snapshots["ledger.journal_lines"][-1][:-1])  # the ledger lost a line
    tie = client.get("/api/v1/posting/tie-out?entity_id=US01", headers=h(AUDITOR)).json()
    assert not tie["ties"]
    assert (tie["items"][0]["postgres"]["lines"], tie["items"][0]["ledger"]["lines"]) == (2, 1)
