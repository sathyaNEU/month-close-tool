"""XLSX import (Gate 7b): a workbook imports exactly like its CSV, and what could hurt the server, or silently
change an amount, is refused before or while it is read."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from journalkit import PREPARER, h, open_period, set_up
from openpyxl import Workbook
from sqlalchemy import Engine, text

from close_app.journals import xlsx

pytestmark = pytest.mark.pg
HEADER = ["journal_key", "template_id", "accounting_date", "description", "account", "side", "amount"]


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)
    open_period(engine)


def workbook(rows: list[list[Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in [HEADER, *rows]:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def as_excel_stores(data: bytes, old: str, new: str) -> bytes:
    """openpyxl writes a float's short form; Excel writes 17 significant digits, so put those back in the sheet."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            body = src.read(item)
            if item.filename == "xl/worksheets/sheet1.xml":
                assert old.encode() in body
                body = body.replace(old.encode(), new.encode(), 1)
            dst.writestr(item, body)
    return out.getvalue()


def upload(client: TestClient, data: bytes) -> Any:
    headers = h(PREPARER) | {"Content-Type": xlsx.CONTENT_TYPE}
    return client.post("/api/v1/journal-imports?entity_id=US01", content=data, headers=headers)


def test_a_workbook_imports_like_its_csv(client: TestClient, engine: Engine, ready: None) -> None:
    day = date(2026, 9, 10)
    data = workbook(
        [
            ["J1", "sample_standard_journal", day, "Carrier accrual true-up", 5020, "DR", Decimal("1234.50")],
            ["J1", None, None, None, 2410, "CR", 1234.5],
        ]
    )
    r = upload(client, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert (out["status"], out["rows"], len(out["journals"]), out["sha256"]) == (
        "created",
        2,
        1,
        hashlib.sha256(data).hexdigest(),  # the workbook's own hash is recorded
    )
    journal = client.get(f"/api/v1/journals/{out['journals'][0]['journal_id']}", headers=h(PREPARER)).json()
    assert [(ln["account"], ln["side"], ln["amount_minor"]) for ln in journal["lines"]] == [
        ("5020", "DR", 123_450),
        ("2410", "CR", 123_450),
    ]
    assert (journal["accounting_date"], journal["source"]) == ("2026-09-10", "import")


def test_what_a_workbook_cannot_do(client: TestClient, engine: Engine, ready: None) -> None:
    formula = workbook([["J1", "sample_standard_journal", "2026-09-10", "Computed amounts", 5020, "DR", "=1+1"]])
    r = upload(client, formula)
    assert r.status_code == 422
    assert "formula has no calculated value" in r.json()["issues"][0]["message"]
    noisy = workbook(
        [
            ["J1", "sample_standard_journal", "2026-09-10", "Floating point amounts", 5020, "DR", 0.3],
            ["J1", None, None, None, 2410, "CR", 0.3],
        ]
    )
    noisy = as_excel_stores(noisy, "<v>0.3</v>", "<v>0.30000000000000004</v>")  # =0.1+0.2, as Excel saves it
    issues = {(i["path"], i["code"]): i["message"] for i in upload(client, noisy).json()["issues"]}
    assert "0.30000000000000004" in issues["/rows/2/amount", "invalid_amount"]  # not rounded to 0.30 behind your back
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml", b"0" * 20_000_000)
    r = upload(client, bomb.getvalue())
    assert (r.status_code, r.json()["issues"][0]["code"]) == (422, "zip_bomb")
    r = upload(client, b"not a workbook at all")
    assert (r.status_code, r.json()["issues"][0]["code"]) == (422, "invalid_workbook")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM journals")).scalar_one() == 0
        statuses = conn.execute(text("SELECT status FROM journal_imports")).scalars().all()
    assert statuses == ["rejected"] * 4  # every refused file is still recorded


def test_one_workbook_is_read_at_a_time(
    client: TestClient, engine: Engine, ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second upload waits for the parsing slot, and gets a 503 (not recorded as an import) if it stays taken."""
    monkeypatch.setattr(xlsx, "SLOT_WAIT_S", 0.1)
    assert xlsx._SLOTS.acquire(timeout=1)
    try:
        busy = upload(client, workbook([["J1", "sample_standard_journal", "2026-09-10", "Busy", 5020, "DR", 1]]))
    finally:
        xlsx._SLOTS.release()
    assert (busy.status_code, busy.json()["title"]) == (503, "Another workbook is being read")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM journal_imports")).scalar_one() == 0
