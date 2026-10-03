"""A foreign-currency journal through the stack (Gate 7c).

1. The local lake gets the generator's daily spot rates (``tiny`` profile) if it has none yet. They are written
   once, and the stack's database and lake outlive a run.
2. alice prepares a EUR journal in US01. The app reads the rate through the narrow lookup over Spark Connect, the
   same rate the ``/fx-rates`` route shows, and freezes the conversion at submit. dave and erin approve.
3. dave posts it. Its ledger lines carry USD amounts converted under D9 (every line half-even at that rate, plus a
   rounding line when they don't balance), the trial balance balances, the tie-out holds, and the stub ERP
   confirms it.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pytest
from stacklib import LAKE_ROOT, User, stack_job
from test_13_journals import PERIOD, _active, _make_active, _post, _sample
from test_14_posting import _lines_of, _post_and_settle, _ready, _status, _ties

from tools import erp_stub

pytestmark = pytest.mark.stack
DDL_APPLY_JOB_ID, DATAGEN_JOB_ID = 1002, 1003
DAY = f"{PERIOD}-15"
LINES = (("5020", "DR", 1_001), ("5020", "DR", 1_001), ("6040", "DR", 3_337), ("2410", "CR", 5_339))


def _rate(user: User) -> dict[str, Any] | None:
    r = user.http.get(f"/api/v1/fx-rates/EUR/USD/{DAY}")
    if r.status_code == 404:
        return None
    assert r.status_code == 200, r.text
    found: dict[str, Any] = r.json()
    return found


def _rates_in_the_lake(alice: User) -> dict[str, Any]:
    rate = _rate(alice)
    if rate is None:
        ddl = stack_job("ddl_apply", DDL_APPLY_JOB_ID, root_uri=LAKE_ROOT)
        assert ddl["result_state"] == "SUCCESS", ddl
        generated = stack_job("datagen", DATAGEN_JOB_ID, profile="tiny", seed="11", root_uri=LAKE_ROOT)
        assert generated["result_state"] == "SUCCESS", generated
        rate = _rate(alice)
    assert rate is not None, "the generator wrote no EUR->USD rate for the day"
    return rate


def _converted(amount_minor: int, rate: str) -> int:
    return int((Decimal(amount_minor) * Decimal(rate)).to_integral_value(rounding=ROUND_HALF_EVEN))


def test_a_euro_journal_is_converted_posted_and_confirmed_on_the_stack() -> None:
    alice, dave, erin, erp = User("alice"), User("dave"), User("erin"), User("erp")
    _ready(alice, dave)
    if not _active(alice, "journal-templates", "sample_fx_journal"):
        _make_active(alice, dave, "journal-templates", _sample("journal-templates-sample_fx_journal"))
    rate = _rates_in_the_lake(alice)
    body = {
        "entity_id": "US01",
        "template_id": "sample_fx_journal",
        "accounting_date": DAY,
        "currency": "EUR",
        "description": f"Euro carrier invoice accrued, stack run {uuid.uuid4().hex[:8]}",
        "lines": [{"account": a, "side": s, "amount_minor": m} for a, s, m in LINES],
    }
    created = _post(alice, "/api/v1/journals", body)
    assert created.status_code == 201, created.text
    j: dict[str, Any] = created.json()
    url = f"/api/v1/journals/{j['journal_id']}"
    j = _post(alice, url + ":submit", etag=j["etag"]).json()
    conversion = j["conversion"]
    assert conversion["rate"]["rate"] == rate["rate"]  # the rate the route shows is the rate the journal froze
    expected = [_converted(m, rate["rate"]) for _, _, m in LINES]
    assert [ln["functional_amount_minor"] for ln in conversion["lines"]] == expected
    while j["status"] == "pending_approval":
        j = _post(dave if j["current_level"] == 1 else erin, url + ":approve", etag=j["etag"]).json()
    assert j["status"] == "approved", j
    run = _post_and_settle(dave)
    assert run["status"] == "reconciled", run
    assert _status(dave, j) == "posted"
    lines = sorted(_lines_of(dave, j), key=lambda ln: ln["line_no"])
    entries = [ln for ln in lines if ln["line_kind"] == "entry"]
    assert [
        (ln["currency"], ln["functional_currency"], ln["amount_minor"], ln["functional_amount_minor"]) for ln in entries
    ] == [("EUR", "USD", m, f) for (_, _, m), f in zip(LINES, expected, strict=True)]
    rounding = [ln for ln in lines if ln["line_kind"] == "fx_rounding"]
    assert len(rounding) == (0 if conversion["rounding"] is None else 1)
    assert (
        sum(ln["debit_minor"] for ln in lines) == sum(ln["credit_minor"] for ln in lines) == j["functional_debit_minor"]
    )
    tb = dave.http.get(f"/api/v1/gl/trial-balance?entity_id=US01&period={PERIOD}").json()
    assert tb["balanced"], tb
    assert _ties(dave)["ties"], _ties(dave)
    summary = erp_stub.run(erp.http, "US01")
    assert summary["refused"] == []
    assert j["journal_id"] in {c["journal_id"] for c in summary["confirmed"]}
