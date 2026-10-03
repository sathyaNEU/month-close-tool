"""Multi-currency manual journals through the app (Gate 7c).

- **D9 at submit.** A foreign-currency journal is converted when it is submitted, frozen per submission, and bound
  into every approval. A later rate never changes it. A missing rate refuses the submit.
- **The lookup** happens after the authorization, ETag and state checks, and outside every transaction.
- **Functional terms.** Routing and bypass caps read the functional total. A reversal reuses its original's rate,
  so the two net to zero. Batches and recurring journals convert the same way.
- **Posting** writes functional amounts, the rate and the rounding line, and the tie-out holds.
- **Elsewhere.** Import reads each currency's decimals. The population shows the conversion. The rate route
  answers. The upgrade backfills live journals, and the downgrade refuses while conversions exist.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from journalkit import (
    APPROVER_1,
    APPROVER_2,
    APPROVER_3,
    AUDITOR,
    CONTROLLER,
    OUTSIDER,
    PERIOD,
    PREPARER,
    PREPARER_2,
    activate,
    body,
    create,
    get,
    h,
    moved,
    open_period,
    rate_rows,
    set_up,
    step,
)
from pgtestkit import ALEMBIC_INI, TEST_PG, read_env
from sqlalchemy import Engine, text
from test_journal_batches import act, acted, add, new_batch
from test_journal_posting import play_job, start_run

from close_app import idempotency
from close_app.context import SCHEMA_HEAD, AppContext
from close_app.journals import tasks as journal_tasks
from close_contracts.config.envelope import ConfigKind
from close_contracts.config.journal_workflow import JournalTemplate, RecurringJournal, RecurringLine
from close_contracts.fx import Rate, RateKey
from close_platform.compute.fake import FakeComputeBackend
from close_platform.outbox import Relay
from close_platform.query import QueryBusyError
from close_platform.query.rates import ReferenceRates
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg
EUR_LINES = (("5020", "DR", 1_001), ("5020", "DR", 1_001), ("2410", "CR", 2_002))  # converts with a cent of rounding
SEPT_15 = RateKey("EUR", "USD", date(2026, 9, 15))


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)
    open_period(engine)
    open_period(engine, period="2026-10")


def euro(client: TestClient, lines: tuple[tuple[str, str, int], ...] = EUR_LINES, **fields: Any) -> dict[str, Any]:
    defaults = {"template_id": "sample_fx_journal", "currency": "EUR", "description": "Euro carrier invoice accrued"}
    return create(client, PREPARER, lines, **(defaults | fields))


def approve_all(client: TestClient, j: dict[str, Any]) -> dict[str, Any]:
    """Approve a submitted journal through every level it needs, one approver per level."""
    while j["status"] == "pending_approval":
        j = moved(client, j, "approve", (APPROVER_1, APPROVER_2, APPROVER_3)[j["current_level"] - 1])
    return j


def submitted(client: TestClient, j: dict[str, Any]) -> dict[str, Any]:
    return moved(client, j, "submit", PREPARER)


# ---------------------------------------------------------------------------------------------- D9 at submit


def test_a_foreign_currency_journal_is_converted_at_submit_and_bound_into_its_approvals(
    client: TestClient, engine: Engine, rates: ReferenceRates, ready: None
) -> None:
    draft = euro(client)
    assert (draft["currency"], draft["functional_currency"], draft["conversion"]) == ("EUR", "USD", None)
    sub = submitted(client, draft)
    conversion = sub["conversion"]
    assert (conversion["rate"]["rate"], conversion["rate"]["rate_date"], conversion["rate"]["version"]) == (
        "1.084213000000",
        "2026-09-15",
        1,
    )
    # 10.01 EUR is 10.85 USD twice, but 20.02 EUR is 21.71 USD: one cent of rounding, on the debit side
    assert [ln["functional_amount_minor"] for ln in conversion["lines"]] == [1_085, 1_085, 2_171]
    assert conversion["rounding"] == {"side": "DR", "amount_minor": 1, "account": "7010"}
    totals = (sub["functional_debit_minor"], sub["functional_credit_minor"], sub["ledger_line_count"])
    assert totals == (2_171, 2_171, 4)
    assert rates.calls == [frozenset({SEPT_15})]  # one lookup, made before the transaction
    done = approve_all(client, sub)
    assert done["status"] == "approved"
    with engine.connect() as conn:
        bound = conn.execute(
            text("SELECT DISTINCT conversion_hash FROM journal_approvals WHERE journal_id = :id"),
            {"id": draft["journal_id"]},
        ).scalars()
        assert list(bound) == [sub["conversion_hash"]]


def test_a_rate_published_later_never_changes_a_submitted_journal(
    client: TestClient, engine: Engine, rates: ReferenceRates, ready: None
) -> None:
    sub = submitted(client, euro(client))
    rates.load(rate_rows({("EUR", "USD"): Decimal("1.200000")}, version=2))  # republished
    unchanged = get(client, sub)
    assert (unchanged["conversion"]["rate"]["rate"], unchanged["conversion_hash"]) == (
        "1.084213000000",
        sub["conversion_hash"],
    )
    back = moved(client, unchanged, "recall", PREPARER)
    assert (back["conversion"], back["conversion_hash"], back["functional_debit_minor"]) == (None, None, None)
    again = submitted(client, back)
    assert (again["conversion"]["rate"]["rate"], again["conversion"]["rate"]["version"]) == ("1.200000000000", 2)
    with engine.connect() as conn:
        frozen = conn.execute(
            text("SELECT submission, rate_version FROM journal_conversions WHERE journal_id = :id ORDER BY submission"),
            {"id": sub["journal_id"]},
        ).all()
    assert [tuple(r) for r in frozen] == [(1, 1), (2, 2)]  # the first submission's conversion is kept


def test_a_missing_rate_refuses_the_submit(client: TestClient, engine: Engine, ready: None) -> None:
    open_period(engine, period="2026-11")
    late = euro(client, accounting_date="2026-11-16")  # after the last published rate
    r = step(client, late, "submit", PREPARER)
    assert (r.status_code, r.json()["issues"][0]["code"]) == (422, "no_rate")
    assert "daily_spot EUR->USD on 2026-11-16" in r.json()["issues"][0]["message"]
    assert get(client, late)["status"] == "draft"


def test_a_changed_conversion_is_caught_when_approving(client: TestClient, admin_engine: Engine, ready: None) -> None:
    sub = submitted(client, euro(client))
    with admin_engine.begin() as tx:  # as the superuser: the app role can't change a frozen conversion at all
        tx.execute(
            text(
                "UPDATE close.journal_conversions SET detail = jsonb_set(detail, '{rate,rate}', "
                "to_jsonb('9.000000000000'::text)) WHERE journal_id = :id"
            ),
            {"id": sub["journal_id"]},
        )
    r = step(client, get(client, sub), "approve", APPROVER_1)
    assert (r.status_code, r.json()["title"]) == (409, "The conversion changed")


# ---------------------------------------------------------------------------------------------- the lookup


class BrokenRates(ReferenceRates):
    def rates(self, keys: Iterable[RateKey]) -> dict[RateKey, Rate]:
        self.calls.append(frozenset(keys))
        raise QueryBusyError("every slot is taken")


class BlockingRates(ReferenceRates):
    def __init__(self) -> None:
        super().__init__(rate_rows())
        self.entered, self.release = threading.Event(), threading.Event()

    def rates(self, keys: Iterable[RateKey]) -> dict[RateKey, Rate]:
        self.entered.set()
        self.release.wait(timeout=10)
        return super().rates(keys)


def test_every_check_comes_before_the_rate_lookup(client: TestClient, ctx: AppContext, ready: None) -> None:
    j = euro(client)
    pending = submitted(client, euro(client))  # with the working rates, before they break
    member, batch = euro(client), new_batch(client)
    batch = add(client, batch, member).json()
    broken = BrokenRates()
    ctx.rates = broken
    url = f"/api/v1/journals/{j['journal_id']}:submit"
    assert step(client, j, "submit", OUTSIDER).status_code == 404  # US01 is invisible to an EU01 preparer
    assert step(client, j, "submit", AUDITOR).status_code == 403  # sees it, can't prepare
    assert client.post(url, headers=h(PREPARER, '"0-0000000000000000"')).status_code == 412  # a stale ETag
    assert client.post(url, headers=h(PREPARER)).status_code == 428  # no ETag
    assert step(client, pending, "submit", PREPARER).status_code == 409  # not a draft
    assert step(client, get(client, member), "submit", PREPARER).status_code == 409  # submitted with its batch
    sod = step(client, j, "submit", PREPARER_2)  # another preparer
    assert (sod.status_code, sod.json()["title"]) == (409, "Separation of duties")
    stale = client.post(
        f"/api/v1/journal-batches/{batch['batch_id']}:submit", headers=h(PREPARER, '"0-0000000000000000"')
    )
    assert stale.status_code == 412
    assert broken.calls == []  # none of those reached the lookup
    busy = step(client, j, "submit", PREPARER)
    assert (busy.status_code, len(broken.calls), get(client, j)["status"]) == (503, 1, "draft")


def test_a_duplicate_submit_that_lost_the_race_replays(
    client: TestClient, ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate whose first look for a stored response came before the first request committed fails its own
    checks (the ETag has moved on), and then answers as the replay it is."""
    j = euro(client)
    headers = h(PREPARER, j["etag"])
    url = f"/api/v1/journals/{j['journal_id']}:submit"
    first = client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    real = idempotency.replay
    looks: list[int] = []

    def too_early(*args: Any, **kwargs: Any) -> Any:  # the first look happens before the first request commits
        looks.append(1)
        return None if len(looks) == 1 else real(*args, **kwargs)

    monkeypatch.setattr(idempotency, "replay", too_early)
    again = client.post(url, headers=headers)
    assert (again.status_code, again.content, again.headers["Idempotent-Replayed"]) == (200, first.content, "true")
    assert len(looks) == 2


def test_the_lookup_holds_no_lock(client: TestClient, ctx: AppContext, engine: Engine, ready: None) -> None:
    j = euro(client)
    blocking = BlockingRates()
    ctx.rates = blocking
    result: dict[str, Any] = {}
    worker = threading.Thread(target=lambda: result.update(r=step(client, j, "submit", PREPARER)))
    worker.start()
    try:
        assert blocking.entered.wait(timeout=10)
        with engine.begin() as tx:  # while the lookup waits, nothing holds the journal or its period
            tx.execute(text("SELECT 1 FROM journals WHERE journal_id = :id FOR UPDATE NOWAIT"), {"id": j["journal_id"]})
            tx.execute(
                text("SELECT 1 FROM accounting_periods WHERE entity_id = 'US01' AND period = :p FOR UPDATE NOWAIT"),
                {"p": PERIOD},
            )
    finally:
        blocking.release.set()
        worker.join(timeout=10)
    assert result["r"].status_code == 200, result["r"].text


# ---------------------------------------------------------------------------------------------- functional terms


def test_routing_uses_the_functional_total(client: TestClient, ready: None) -> None:
    sub = submitted(client, euro(client, lines=(("5020", "DR", 950_000), ("2410", "CR", 950_000))))
    # 9,500 EUR is under the USD level-2 threshold of 10,000 as a number, but it is 10,300.02 USD
    assert (sub["functional_debit_minor"], sub["routing"]["total_minor"], sub["routing"]["amount_levels"]) == (
        1_030_002,
        1_030_002,
        2,
    )


def test_reversals_reuse_the_original_rate_and_net_to_zero(
    client: TestClient, engine: Engine, rates: ReferenceRates, ready: None
) -> None:
    original = approve_all(client, submitted(client, euro(client)))
    looked_up = len(rates.calls)
    reversal = submitted(client, moved(client, original, "reverse", PREPARER))  # dated 2026-10-01
    assert len(rates.calls) == looked_up  # no lookup: the original's rate
    assert reversal["conversion"]["rate"] == original["conversion"]["rate"]
    assert reversal["conversion"]["reused_from"] == original["journal_id"]
    net: dict[str, int] = {}
    for j in (original, reversal):
        for line, converted in zip(j["lines"], j["conversion"]["lines"], strict=True):
            sign = 1 if line["side"] == "DR" else -1
            net[line["account"]] = net.get(line["account"], 0) + sign * converted["functional_amount_minor"]
        rounding = j["conversion"]["rounding"]
        sign = 1 if rounding["side"] == "DR" else -1
        net[rounding["account"]] = net.get(rounding["account"], 0) + sign * rounding["amount_minor"]
    assert set(net.values()) == {0}  # every account, the rounding account included
    # An edited reversal is a new journal in substance: it converts at its own date's rate.
    second = approve_all(client, submitted(client, euro(client)))
    draft = moved(client, second, "reverse", PREPARER)
    edited_body = body(
        ("2410", "DR", 2_000), ("5020", "CR", 2_000),
        template_id="sample_fx_journal", currency="EUR", accounting_date="2026-10-01", description="Edited reversal",
    )  # fmt: skip
    edited = client.put(f"/api/v1/journals/{draft['journal_id']}", json=edited_body, headers=h(PREPARER, draft["etag"]))
    assert edited.status_code == 200, edited.text
    fresh = submitted(client, edited.json())
    assert fresh["conversion"]["reused_from"] is None
    assert rates.calls[-1] == frozenset({RateKey("EUR", "USD", date(2026, 10, 1))})


def test_an_automatic_reversal_bypasses_at_the_original_rate(client: TestClient, engine: Engine, ready: None) -> None:
    template = JournalTemplate(
        template_id="test_fx_accrual",
        name="Euro accrual",
        min_description_length=10,
        auto_reverse=True,
        currencies=("EUR",),
    )
    activate(engine, ConfigKind.JOURNAL_TEMPLATE, template.template_id, template)
    original = approve_all(client, submitted(client, euro(client, template_id="test_fx_accrual")))
    items = client.get("/api/v1/journals?entity_id=US01", headers=h(PREPARER)).json()["items"]
    reversal = get(client, next(j for j in items if j["reverses_journal_id"] == original["journal_id"]))
    assert (reversal["status"], reversal["accounting_date"]) == ("approved", "2026-10-01")
    assert reversal["bypass_reason"].startswith("automatic reversal")  # 21.71 USD is under the 5,000 USD cap
    assert reversal["conversion"]["rate"] == original["conversion"]["rate"]
    assert reversal["functional_debit_minor"] == original["functional_debit_minor"]


def test_a_batch_converts_every_member_and_one_missing_rate_refuses_it(
    client: TestClient, engine: Engine, ready: None
) -> None:
    open_period(engine, period="2026-11")
    late = new_batch(client)
    stuck = (euro(client), euro(client, accounting_date="2026-11-16"))
    for j in stuck:
        late = add(client, late, j).json()
    refused = act(client, late, "submit", PREPARER)
    assert (refused.status_code, [i["code"] for i in refused.json()["issues"]]) == (422, ["no_rate"])
    assert [get(client, j)["status"] for j in stuck] == ["draft", "draft"]  # all or none
    batch = new_batch(client)
    members = (euro(client), create(client, PREPARER, EUR_LINES, template_id="sample_fx_journal", currency="GBP",
                                    description="Pound carrier invoice accrued"))  # fmt: skip
    for j in members:
        batch = add(client, batch, j).json()
    batch = acted(client, batch, "submit", PREPARER)
    converted = [get(client, j)["conversion"]["rate"] for j in members]
    assert [(r["from_currency"], r["rate"]) for r in converted] == [
        ("EUR", "1.084213000000"),
        ("GBP", "1.271502000000"),
    ]
    for user in (APPROVER_1, APPROVER_2, APPROVER_3):  # as many levels as the batch needs
        if batch["status"] == "pending_approval":
            batch = acted(client, batch, "approve", user)
    assert batch["status"] == "approved"
    with engine.connect() as conn:
        for j in members:  # every member's approvals are bound to its own conversion
            hashes = set(
                conn.execute(
                    text("SELECT conversion_hash FROM journal_approvals WHERE journal_id = :id"),
                    {"id": j["journal_id"]},
                ).scalars()
            )
            assert hashes == {get(client, j)["conversion_hash"]}


def test_an_auto_approved_recurring_journal_converts_when_its_rate_is_published(
    client: TestClient, engine: Engine, rates: ReferenceRates, ready: None
) -> None:
    recurring = RecurringJournal(
        recurring_id="test_fx_fee",
        template_id="sample_fx_journal",
        entity_id="US01",
        frequency="monthly",
        day=20,
        first_period="2026-09",
        last_period="2026-09",
        preparer=PREPARER,
        description="Monthly euro service fee",
        lines=(
            RecurringLine(account="5020", side="DR", amount_minor=1_001),
            RecurringLine(account="2410", side="CR", amount_minor=1_001),
        ),
        auto_approve=True,
        currency="EUR",
    )
    activate(engine, ConfigKind.RECURRING_JOURNAL, recurring.recurring_id, recurring)
    at = datetime(2026, 9, 21, tzinfo=UTC)
    assert journal_tasks.run_recurring(engine, as_of=at, rates=ReferenceRates()) == 0  # no rate yet: skipped
    with engine.connect() as conn:
        note = conn.execute(text("SELECT message FROM notifications WHERE kind = 'recurring_skipped'")).scalar_one()
    assert "no daily_spot EUR->USD on 2026-09-20 rate has been published yet" in note
    assert journal_tasks.run_recurring(engine, as_of=at, rates=rates) == 1
    items = client.get("/api/v1/journals?entity_id=US01", headers=h(PREPARER)).json()["items"]
    created = get(client, next(j for j in items if j["recurring_id"] == "test_fx_fee"))
    assert (created["status"], created["currency"], created["functional_debit_minor"]) == ("approved", "EUR", 1_085)
    assert created["conversion"]["rounding"] is None


# ---------------------------------------------------------------------------------------------- posting and evidence


def test_posting_writes_functional_amounts_the_rate_and_the_rounding_line(
    client: TestClient,
    engine: Engine,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    ready: None,
) -> None:
    j = approve_all(client, submitted(client, euro(client)))
    relay = Relay(engine, ctx.coordinator.handlers(), instance_id="test-relay")
    play_job(engine, relay, ctx, fake, lake, start_run(client).json()["run_id"])
    mine = sorted(
        (ln for ln in lake.snapshots["ledger.journal_lines"][-1] if ln["journal_id"] == j["journal_id"]),
        key=lambda ln: ln["line_no"],
    )
    assert [
        (ln["line_no"], ln["line_kind"], ln["account"], ln["side"], ln["amount_minor"], ln["functional_amount_minor"])
        for ln in mine
    ] == [
        (1, "entry", "5020", "DR", 1_001, 1_085),
        (2, "entry", "5020", "DR", 1_001, 1_085),
        (3, "entry", "2410", "CR", 2_002, 2_171),
        (4, "fx_rounding", "7010", "DR", 0, 1),
    ]
    assert {(ln["currency"], ln["functional_currency"], ln["fx_rate"], ln["fx_rate_version"]) for ln in mine} == {
        ("EUR", "USD", Decimal("1.084213000000"), 1)
    }
    tb = client.get(f"/api/v1/gl/trial-balance?entity_id=US01&period={PERIOD}", headers=h(AUDITOR)).json()
    assert (tb["debits"], tb["credits"], tb["balanced"]) == (2_171, 2_171, True)
    tie = client.get("/api/v1/posting/tie-out?entity_id=US01", headers=h(AUDITOR)).json()
    assert tie["ties"], tie
    assert tie["items"][0]["postgres"] == {"lines": 4, "debits": 2_171, "credits": 2_171}
    feed = next(f for f in lake.snapshots["ledger.journal_postings"][-1] if f["journal_id"] == j["journal_id"])
    payload = json.loads(feed["payload"])
    assert (payload["currency"], payload["functional_currency"]) == ("EUR", "USD")
    assert payload["rate"]["rate"] == "1.084213000000"
    assert [ln["kind"] for ln in payload["lines"]] == ["entry", "entry", "entry", "fx_rounding"]


def test_an_import_reads_each_currency_with_its_own_decimals(client: TestClient, engine: Engine, ready: None) -> None:
    template = JournalTemplate(template_id="test_fx_any", name="Any currency", currencies=("JPY", "KWD", "EUR"))
    activate(engine, ConfigKind.JOURNAL_TEMPLATE, template.template_id, template)
    header = "journal_key,template_id,accounting_date,description,account,side,amount,currency\n"

    def upload(rows: str) -> Any:
        headers = h(PREPARER) | {"Content-Type": "text/csv"}
        return client.post("/api/v1/journal-imports?entity_id=US01", content=(header + rows).encode(), headers=headers)

    good = upload(
        "Y1,test_fx_any,2026-09-10,Yen charge,5020,DR,1500,JPY\nY1,,,,2410,CR,1500,\n"
        "K1,test_fx_any,2026-09-10,Dinar charge,5020,DR,1.234,KWD\nK1,,,,2410,CR,1.234,\n"
    )
    assert good.status_code == 201, good.text
    journals = [get(client, j) for j in good.json()["journals"]]
    assert [(j["currency"], j["lines"][0]["amount_minor"]) for j in journals] == [("JPY", 1_500), ("KWD", 1_234)]
    bad = upload(
        "Y1,test_fx_any,2026-09-10,Yen charge,5020,DR,12.5,JPY\nY1,,,,2410,CR,12.5,\n"
        "Q1,test_fx_any,2026-09-10,Unknown,5020,DR,1,QQQ\n"
    )
    assert {(i["path"], i["code"]) for i in bad.json()["issues"]} >= {
        ("/rows/2/amount", "invalid_amount"),
        ("/rows/3/amount", "invalid_amount"),
        ("/rows/4/currency", "unknown_currency"),
    }


def test_the_population_shows_the_conversion(client: TestClient, ready: None) -> None:
    sub = submitted(client, euro(client))
    exported = client.get("/api/v1/journal-population?entity_id=US01", headers=h(CONTROLLER)).text
    row = next(r for r in csv.DictReader(io.StringIO(exported)) if r["journal_id"] == sub["journal_id"])
    assert {k: row[k] for k in ("currency", "functional_currency", "functional_debit_minor", "ledger_line_count",
            "fx_rate", "fx_rate_date", "fx_rate_version", "fx_rate_source", "fx_rounding_minor")} == {
        "currency": "EUR", "functional_currency": "USD", "functional_debit_minor": "2171", "ledger_line_count": "4",
        "fx_rate": "1.084213000000", "fx_rate_date": "2026-09-15", "fx_rate_version": "1",
        "fx_rate_source": "reference", "fx_rounding_minor": "1",
    }  # fmt: skip


def test_the_rate_route(client: TestClient, ready: None) -> None:
    ok = client.get("/api/v1/fx-rates/EUR/USD/2026-09-15", headers=h(PREPARER))
    assert (ok.status_code, ok.json()["rate"], ok.json()["source"]) == (200, "1.084213000000", "reference")
    assert client.get("/api/v1/fx-rates/EUR/USD/2026-12-01", headers=h(PREPARER)).status_code == 404
    assert client.get("/api/v1/fx-rates/EUR/EUR/2026-09-15", headers=h(PREPARER)).status_code == 422
    assert client.get("/api/v1/fx-rates/EUR/USD/2026-09-15?rate_type=monthly", headers=h(PREPARER)).status_code == 422
    assert client.get("/api/v1/fx-rates/EUR/USD/2026-09-15", headers=h("nobody")).status_code == 403


# ---------------------------------------------------------------------------------------------- the upgrade


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **read_env("pg-migrator.env"), "PGHOST": TEST_PG["host"], "PGPORT": TEST_PG["port"]}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_upgrade_backfills_live_journals_and_they_still_approve(
    client: TestClient, engine: Engine, admin_engine: Engine, ready: None
) -> None:
    pending = submitted(client, create(client))
    done = approve_all(client, submitted(client, create(client)))
    try:
        assert _alembic("downgrade", "0005").returncode == 0  # as before Stage 7c: no functional columns
        assert _alembic("upgrade", "head").returncode == 0
    finally:
        _alembic("upgrade", "head")
    engine.dispose()  # pooled connections may hold plans for the old columns
    for j in (pending, done):
        again = get(client, j)
        assert (again["functional_debit_minor"], again["ledger_line_count"], again["functional_currency"]) == (
            j["total_debit_minor"],
            j["line_count"],
            "USD",
        )
    assert approve_all(client, get(client, pending))["status"] == "approved"  # its content hash still matches


def test_the_downgrade_refuses_while_conversions_exist(client: TestClient, admin_engine: Engine, ready: None) -> None:
    submitted(client, euro(client))
    try:
        refused = _alembic("downgrade", "0005")
        assert refused.returncode != 0
        assert "foreign-currency journals exist" in refused.stderr
    finally:
        _alembic("upgrade", "head")
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM close.alembic_version")).scalar_one() == SCHEMA_HEAD
