"""CSV import and the evidence for auditors (Gate 7a): an import is all or nothing with row-numbered issues, numbers
are gapless per entity, the population export ties to the journal table, and the exception report lists what
went wrong or skipped approval."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient
from journalkit import APPROVER_1, AUDITOR, CONTROLLER, PREPARER, body, create, h, moved, open_period, set_up
from sqlalchemy import Engine, text

pytestmark = pytest.mark.pg

GOOD = "\n".join(
    (
        "journal_key,template_id,accounting_date,description,account,side,amount,line_description,"
        "header:reference,value:reason_code,dim:program",
        "J1,sample_standard_journal,2026-09-10,Carrier accrual true-up,5020,DR,1234.50,September labels,TRUEUP-9,,",
        "J1,,,,2410,CR,1234.50,,,,",
        "J2,sample_reclass,2026-09-11,Fees booked to the wrong line,6030,DR,10.00,,,misposting,",
        "J2,sample_reclass,2026-09-11,Fees booked to the wrong line,5030,CR,10.00,,,misposting,",
        "",
    )
)
BAD = """journal_key,template_id,accounting_date,description,account,side,amount
J1,sample_standard_journal,2026-09-10,Carrier accrual true-up,5020,DR,10.00
J1,,,,2410,CR,9.00
J2,sample_standard_journal,2026-09-10,Unknown account used here,0000,DR,5.00
J2,,,,2410,CR,5.00
J3,sample_standard_journal,2026-10-03,October isn't open yet,5020,DR,5.00
J3,,,,2410,CR,5.00
J4,sample_standard_journal,2026-09-10,Too many decimals here,5020,DR,5.001
"""


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)
    open_period(engine)


def upload(client: TestClient, content: str, user: str = PREPARER) -> object:
    headers = h(user) | {"Content-Type": "text/csv"}
    return client.post("/api/v1/journal-imports?entity_id=US01", content=content.encode(), headers=headers)


def test_a_valid_file_creates_every_journal(client: TestClient, ready: None) -> None:
    r = upload(client, GOOD)
    assert r.status_code == 201, r.text  # type: ignore[attr-defined]
    out = r.json()  # type: ignore[attr-defined]
    assert (out["status"], out["rows"], len(out["journals"])) == ("created", 4, 2)
    items = client.get("/api/v1/journals?entity_id=US01", headers=h(PREPARER)).json()["items"]
    by_number = {j["number"]: j for j in items}
    assert {j["source"] for j in items} == {"import"}
    first = client.get(f"/api/v1/journals/{by_number[1]['journal_id']}", headers=h(PREPARER)).json()
    assert (first["header"], [ln["amount_minor"] for ln in first["lines"]]) == (
        {"reference": "TRUEUP-9"},
        [123_450, 123_450],
    )
    second = client.get(f"/api/v1/journals/{by_number[2]['journal_id']}", headers=h(PREPARER)).json()
    assert [ln["values"] for ln in second["lines"]] == [{"reason_code": "misposting"}] * 2
    assert upload(client, GOOD).status_code == 201  # type: ignore[attr-defined]  # a new key imports again...
    record = client.get(f"/api/v1/journal-imports/{out['import_id']}", headers=h(PREPARER)).json()
    assert (record["status"], record["journal_count"]) == ("created", 2)


def test_a_bad_file_creates_nothing_and_says_where(client: TestClient, engine: Engine, ready: None) -> None:
    r = upload(client, BAD)
    assert r.status_code == 422  # type: ignore[attr-defined]
    issues = {(i["path"], i["code"]) for i in r.json()["issues"]}  # type: ignore[attr-defined]
    assert issues == {
        ("/rows/2/lines", "unbalanced"),  # a journal's own problem sits on its first row
        ("/rows/4/account", "unknown_account"),  # a line's problem on that line's row
        ("/rows/6/accounting_date", "period_not_open"),
        ("/rows/8/amount", "invalid_amount"),
        ("/rows/8/lines", "too_short"),  # its only line was refused, so the journal has none
    }
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM journals")).scalar_one() == 0
        assert conn.execute(text("SELECT status FROM journal_imports")).scalar_one() == "rejected"
    headers_only = upload(client, "journal_key,account\n")
    assert headers_only.status_code == 422  # type: ignore[attr-defined]
    assert headers_only.json()["issues"][0]["code"] == "bad_columns"  # type: ignore[attr-defined]
    exceptions = client.get("/api/v1/journal-exceptions?entity_id=US01", headers=h(AUDITOR)).json()["items"]
    assert [e["kind"] for e in exceptions] == ["import_rejected", "import_rejected"]


def test_numbers_are_gapless_and_the_population_ties_out(client: TestClient, ready: None) -> None:
    first = create(client)
    bad = client.post("/api/v1/journals", json=body(("5020", "DR", 1), ("2410", "CR", 2)), headers=h(PREPARER))
    assert bad.status_code == 422  # a refused journal takes no number
    second = create(client)
    assert (first["number"], second["number"]) == (1, 2)
    moved(client, moved(client, second, "submit", PREPARER), "approve", APPROVER_1)
    moved(client, first, "cancel", PREPARER)  # a cancelled journal stays in the population
    third = create(client)
    assert third["number"] == 3
    summary = client.get("/api/v1/journal-population/summary?entity_id=US01", headers=h(AUDITOR)).json()["items"]
    assert summary == [
        {
            "entity_id": "US01",
            "journals": 3,
            "first_number": 1,
            "last_number": 3,
            "gapless": True,
            "counter_agrees": True,
            "by_status": {"cancelled": 1, "approved": 1, "draft": 1},
        }
    ]
    exported = client.get("/api/v1/journal-population?entity_id=US01", headers=h(CONTROLLER))
    assert exported.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert [(r["number"], r["status"]) for r in rows] == [("1", "cancelled"), ("2", "approved"), ("3", "draft")]
    assert rows[1]["approvals"] == f"1:{APPROVER_1}"
    assert rows[1]["risk_factors"] == "infrequent_preparer;seldom_used_account"
    assert client.get("/api/v1/journal-population?entity_id=US01", headers=h(PREPARER)).status_code == 403
    cancelled = client.get("/api/v1/journal-exceptions?entity_id=US01", headers=h(AUDITOR)).json()["items"]
    assert [(e["kind"], e["number"]) for e in cancelled] == [("cancel", 1)]


def test_the_export_neutralises_spreadsheet_formulas(client: TestClient, ready: None) -> None:
    create(client, description='=HYPERLINK("http://example.invalid","click")')
    exported = client.get("/api/v1/journal-population?entity_id=US01", headers=h(AUDITOR)).text
    [row] = list(csv.DictReader(io.StringIO(exported)))
    assert row["description"] == "'" + '=HYPERLINK("http://example.invalid","click")'
