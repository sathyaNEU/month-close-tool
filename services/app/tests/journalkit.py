"""Helpers for the journal workflow tests (Stage 7a): the SAMPLE configuration made active directly, people with
journal roles, periods, and HTTP helpers."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from apptestkit import assign
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Engine, text

from close_contracts.config.envelope import ConfigKind, spec_hash
from close_contracts.samples.journal_workflow import SAMPLE_APPROVAL_MATRIX, SAMPLE_ENTITIES, SAMPLE_TEMPLATES
from close_contracts.samples.journaling import SAMPLE_CHART, SAMPLE_DIMENSIONS

PREPARER, APPROVER_1, APPROVER_2, APPROVER_3 = "7001", "7002", "7003", "7004"
CONTROLLER, AUDITOR, OUTSIDER, PREPARER_2 = "7005", "7006", "7007", "7008"
ROLES = {
    PREPARER: ("journal_preparer", "US01"),
    PREPARER_2: ("journal_preparer", "US01"),
    APPROVER_1: ("journal_approver_l1", "US01"),
    APPROVER_2: ("journal_approver_l2", "US01"),
    APPROVER_3: ("journal_approver_l3", "US01"),
    CONTROLLER: ("controller", "US01"),
    AUDITOR: ("auditor", "US01"),
    OUTSIDER: ("journal_preparer", "EU01"),
}
PERIOD = "2026-09"
DAY = "2026-09-15"
# Stage 7c: the reference rates every app test's lookup serves (daily spot, each day of September and October).
SAMPLE_RATES: Final = {
    ("EUR", "USD"): Decimal("1.084213"),
    ("GBP", "USD"): Decimal("1.271502"),
    ("USD", "EUR"): Decimal("0.922328"),
    ("JPY", "USD"): Decimal("0.006812"),
    ("KWD", "USD"): Decimal("3.254100"),
}


def rate_rows(rates: dict[tuple[str, str], Decimal] = SAMPLE_RATES, version: int = 1) -> list[dict[str, Any]]:
    days = [date(2026, 9, d) for d in range(1, 31)] + [date(2026, 10, d) for d in range(1, 32)]
    return [
        {"rate_date": day, "from_currency": src, "to_currency": dst, "rate_type": "daily_spot", "rate": rate,
         "version": version, "source": "reference"}
        for day in days
        for (src, dst), rate in rates.items()
    ]  # fmt: skip


US_FED_2026: Final = frozenset(  # the 2026 US federal holidays the tests' calendar knows (Stage 8b)
    {date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26),
     date(2026, 12, 25)}
)  # fmt: skip


def calendar_days(holidays: frozenset[date] = US_FED_2026) -> dict[date, bool]:
    """Every day from June 2026 to February 2027, and whether it is a business day (weekdays but holidays)."""
    day, out = date(2026, 6, 1), {}
    while day <= date(2027, 2, 28):
        out[day] = day.weekday() < 5 and day not in holidays
        day += timedelta(days=1)
    return out


def activate(engine: Engine, kind: ConfigKind, config_id: str, model: BaseModel, *, version: int = 1) -> None:
    """Make a configuration version active directly (the maker-checker API has its own tests)."""
    entities = [model.entity_id] if kind == ConfigKind.RECURRING_JOURNAL else []  # type: ignore[attr-defined]
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO config_versions (kind, config_id, version, status, effective_from, spec, content_hash, "
                "entity_ids, authors, created_by, activated_by, activated_at) VALUES (:k, :c, :v, 'active', "
                "'2026-06-01', CAST(:spec AS jsonb), :h, :e, '{seed}', 'seed', 'seed-approver', now())"
            ),
            {
                "k": kind.value,
                "c": config_id,
                "v": version,
                "spec": json.dumps(model.model_dump(mode="json")),
                "h": spec_hash(model),
                "e": entities,
            },
        )


def set_up(engine: Engine, *, matrix: BaseModel = SAMPLE_APPROVAL_MATRIX) -> None:
    activate(engine, ConfigKind.ENTITY_SET, SAMPLE_ENTITIES.entity_set_id, SAMPLE_ENTITIES)
    activate(engine, ConfigKind.CHART_OF_ACCOUNTS, SAMPLE_CHART.chart_id, SAMPLE_CHART)
    activate(engine, ConfigKind.DIMENSION_SET, SAMPLE_DIMENSIONS.dimension_set_id, SAMPLE_DIMENSIONS)
    for t in SAMPLE_TEMPLATES:
        activate(engine, ConfigKind.JOURNAL_TEMPLATE, t.template_id, t)
    activate(engine, ConfigKind.APPROVAL_MATRIX, SAMPLE_APPROVAL_MATRIX.matrix_id, matrix)
    for user, (role, scope) in ROLES.items():
        assign(engine, user, role, scope)


def open_period(engine: Engine, entity_id: str = "US01", period: str = PERIOD, status: str = "open") -> None:
    year, month = int(period[:4]), int(period[5:])
    last = (date(year + month // 12, month % 12 + 1, 1) - date.resolution).isoformat()
    closed = "clock_timestamp()" if status != "open" else "NULL"
    locked = "clock_timestamp()" if status == "locked" else "NULL"
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO accounting_periods (entity_id, period, starts_on, ends_on, status, opened_by, closed_at, "
                f"locked_at) VALUES (:e, :p, :s, :end, :st, 'seed', {closed}, {locked})"
            ),
            {"e": entity_id, "p": period, "s": f"{period}-01", "end": last, "st": status},
        )


def h(user: str, etag: str | None = None) -> dict[str, str]:
    headers = {"X-Forwarded-User": user, "X-Forwarded-Email": f"{user}@x", "Idempotency-Key": f"k-{uuid.uuid4()}"}
    return headers | ({"If-Match": etag} if etag else {})


def body(*lines: tuple[str, str, int], **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "entity_id": "US01",
        "template_id": "sample_standard_journal",
        "accounting_date": DAY,
        "description": "Reclass of a September carrier charge",
        "lines": [
            {"account": a, "side": s, "amount_minor": m}
            for a, s, m in (lines or (("5020", "DR", 12_345), ("2410", "CR", 12_345)))
        ],
    }
    return base | fields


def create(
    client: TestClient, user: str = PREPARER, lines: tuple[tuple[str, str, int], ...] = (), **fields: Any
) -> dict[str, Any]:
    r = client.post("/api/v1/journals", json=body(*lines, **fields), headers=h(user))
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def step(client: TestClient, journal: dict[str, Any], verb: str, user: str, **payload: Any) -> Any:
    url = f"/api/v1/journals/{journal['journal_id']}:{verb}"
    return client.post(url, json=payload or None, headers=h(user, journal["etag"]))


def moved(client: TestClient, journal: dict[str, Any], verb: str, user: str, **payload: Any) -> dict[str, Any]:
    r = step(client, journal, verb, user, **payload)
    assert r.status_code in (200, 201), (verb, r.status_code, r.text)
    out: dict[str, Any] = r.json()
    return out


def get(client: TestClient, journal: dict[str, Any], user: str = PREPARER) -> dict[str, Any]:
    r = client.get(f"/api/v1/journals/{journal['journal_id']}", headers=h(user))
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def approved(client: TestClient, journal: dict[str, Any]) -> dict[str, Any]:
    """Submit and approve a journal through as many levels as it needs, one approver per level."""
    j = moved(client, journal, "submit", journal["created_by"])
    approvers = [a for a in (APPROVER_1, APPROVER_2, APPROVER_3) if a != journal["created_by"]]
    while j["status"] == "pending_approval":
        level = j["current_level"]
        user = next(a for a in approvers[level - 1 :] if a not in {x["approver"] for x in j["approvals"]})
        j = moved(client, j, "approve", user)
    return j
