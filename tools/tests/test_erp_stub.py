"""The stub ERP (Stage 7b) against a scripted API: it pages through the feed, numbers each balanced journal from its
entity and number, refuses an unbalanced payload, and writes back at most 500 entries per request."""

from __future__ import annotations

import json
from typing import Any

import httpx

from tools import erp_stub


def _posting(number: int, debit: int, credit: int) -> dict[str, Any]:
    lines = [{"side": "DR", "amount_minor": debit}, {"side": "CR", "amount_minor": credit}]
    return {
        "journal_id": f"j{number}",
        "entity_id": "US01",
        "number": number,
        "payload": json.dumps({"lines": lines}),
    }


def test_the_stub_books_balanced_journals_and_writes_their_numbers_back() -> None:
    pages = {
        None: ([_posting(n, 100, 100) for n in range(1, 501)], "page-2"),
        "page-2": ([_posting(501, 100, 100), _posting(502, 100, 90)], None),
    }
    written: list[list[dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/gl/postings":
            assert request.url.params["entity_id"] == "US01"
            rows, cursor = pages[request.url.params.get("cursor")]
            return httpx.Response(200, json={"postings": rows, "next_cursor": cursor})
        assert request.url.path == "/api/v1/posting/status"
        assert request.headers["Idempotency-Key"].startswith("erp-")
        entries = json.loads(request.content)["entries"]
        written.append(entries)
        return httpx.Response(200, json={"confirmed": entries})

    with httpx.Client(base_url="http://erp.test", transport=httpx.MockTransport(handler)) as client:
        summary = erp_stub.run(client, "US01")
    assert (summary["read"], len(summary["confirmed"]), summary["refused"]) == (502, 501, ["j502"])
    assert [len(w) for w in written] == [500, 1]
    assert written[0][0] == {"journal_id": "j1", "document_id": "ERP-US01-000001"}
    assert written[1] == [{"journal_id": "j501", "document_id": "ERP-US01-000501"}]


def test_a_foreign_currency_journal_balances_in_both_currencies() -> None:
    lines = [
        {"side": "DR", "amount_minor": 1_001, "functional_amount_minor": 1_335},
        {"side": "DR", "amount_minor": 1_001, "functional_amount_minor": 1_335},
        {"side": "CR", "amount_minor": 2_002, "functional_amount_minor": 2_669},
        {"side": "CR", "amount_minor": 0, "functional_amount_minor": 1, "kind": "fx_rounding"},
    ]
    posting = {"journal_id": "j1", "entity_id": "US01", "number": 1, "payload": json.dumps({"lines": lines})}
    assert erp_stub.balanced(posting)
    assert not erp_stub.balanced(posting | {"payload": json.dumps({"lines": lines[:3]})})  # no rounding line


def test_the_stub_trial_balance_is_the_gl_plus_erp_only_entries() -> None:
    balances = {
        "accounts": [
            {"account": "1110", "closing_minor": 150_005},
            {"account": "4010", "closing_minor": -150_005},
            {"account": "5020", "closing_minor": 0},
        ]
    }
    text = erp_stub.trial_balance_csv(balances, {"6050": 100, "2420": -100, "1110": -5}, decimals=2)
    assert text == "account,balance\n1110,1500.00\n2420,-1.00\n4010,-1500.05\n6050,1.00\n"
    assert erp_stub.trial_balance_csv({"accounts": [{"account": "1", "closing_minor": -7}]}, {}, decimals=0) == (
        "account,balance\n1,-7\n"
    )
