"""Flux analysis in the control plane (Gate 8b).

- **Lines** compare closing balances (asset, liability and equity accounts) or the period's activity (revenue and
  expense accounts) with the prior period, from both GL sources. Thresholds apply per type, per account override,
  per currency and per operator, and a change from zero is over any percent.
- **Explanations** are bound to the amounts the server computed, and go stale when those change.
- **The review** needs every line over its threshold explained, by people other than the reviewer. Its hash binds
  it to the lines and explanations it saw, so the ``flux_reviewed`` check fails after a later change.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from journalkit import APPROVER_1, AUDITOR, CONTROLLER, PERIOD, PREPARER, activate, h, open_period, set_up
from sqlalchemy import Engine
from test_ledger import manual, rollup

from close_contracts.config.close import CloseChecklist, CloseTask, FluxRules, FluxThreshold
from close_contracts.config.envelope import ConfigKind
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg

RULES = FluxRules(
    rules_id="test_flux",
    by_type={
        **{t: FluxThreshold(amounts={"USD": 1_000, "EUR": 1_000, "GBP": 1_000}, percent=Decimal(10))
           for t in ("asset", "liability", "equity")},
        **{t: FluxThreshold(amounts={"USD": 500, "EUR": 500, "GBP": 500}, percent=Decimal(50), operator="or")
           for t in ("revenue", "expense")},
    },
    accounts={"1120": FluxThreshold(amounts={"USD": 0, "EUR": 0, "GBP": 0}, percent=Decimal(0), operator="or")},
)  # fmt: skip
ROWS = [
    rollup("2026-08", "1110", "DR", 10_000),  # 10,000 -> 10,500: +5%, under 1,000 ("and")
    rollup(PERIOD, "1110", "DR", 500),
    rollup("2026-08", "1130", "DR", 1_000),  # 1,000 -> 6,000: +500% and +5,000
    rollup(PERIOD, "1130", "DR", 5_000),
    rollup("2026-08", "1120", "DR", 50),  # 50 -> 51: the override explains any change
    rollup(PERIOD, "1120", "DR", 1),
    rollup("2026-08", "4010", "CR", 10_000),  # activity -10,000 -> -10,400: under 500 and under 50% ("or")
    rollup(PERIOD, "4010", "CR", 10_400),
    rollup(PERIOD, "4020", "CR", 100),  # nothing in August: over any percent
]


@pytest.fixture
def ready(engine: Engine, lake: ReferenceBackend) -> ReferenceBackend:
    set_up(engine)
    open_period(engine, period="2026-08")
    open_period(engine)
    activate(engine, ConfigKind.FLUX_RULES, RULES.rules_id, RULES)
    lake.load("ledger.gl_rollup_lines", ROWS)
    lake.load("ledger.journal_lines", [manual(PERIOD, "5020", "DR", 30, 1), manual(PERIOD, "2410", "CR", 30, 2)])
    return lake


def flux(client: TestClient, user: str = AUDITOR) -> dict[str, Any]:
    found: dict[str, Any] = client.get(f"/api/v1/flux?entity_id=US01&period={PERIOD}", headers=h(user)).json()
    return found


def explain(client: TestClient, account: str, user: str = PREPARER, text: str = "a new seller program") -> Any:
    body = {"entity_id": "US01", "period": PERIOD, "account": account, "explanation": text}
    return client.post("/api/v1/flux/explanations", json=body, headers=h(user))


def review(client: TestClient, user: str) -> Any:
    return client.post("/api/v1/flux/reviews", json={"entity_id": "US01", "period": PERIOD}, headers=h(user))


def test_each_account_is_compared_with_the_prior_period(client: TestClient, ready: ReferenceBackend) -> None:
    out = flux(client)
    assert (out["comparison_period"], out["currency"], out["rules"]) == (
        "2026-08",
        "USD",
        {"rules_id": "test_flux", "version": 1},
    )
    lines = {
        line["account"]: (
            line["measure"], line["current_minor"], line["comparison_minor"], line["percent"],
            line["needs_explanation"],
        )
        for line in out["lines"]
    }  # fmt: skip
    assert lines == {
        "1110": ("closing", 10_500, 10_000, "5.00", False),
        "1120": ("closing", 51, 50, "2.00", True),
        "1130": ("closing", 6_000, 1_000, "500.00", True),
        "2410": ("closing", -30, 0, None, False),  # the posted manual journals count too; under 1,000 ("and")
        "4010": ("activity", -10_400, -10_000, "-4.00", False),
        "4020": ("activity", -100, 0, None, True),
        "5020": ("activity", 30, 0, None, True),
    }
    assert (out["needing_explanation"], out["explained"], out["unexplained"]) == (
        4,
        0,
        ["1120", "1130", "4020", "5020"],
    )
    assert client.get(f"/api/v1/flux?entity_id=EU01&period={PERIOD}", headers=h(AUDITOR)).status_code == 404


def test_an_explanation_is_bound_to_the_servers_amounts_and_goes_stale(
    client: TestClient, ready: ReferenceBackend
) -> None:
    made = explain(client, "1130", text="a seller's consignment arrived")
    assert made.status_code == 201, made.text
    assert {k: made.json()[k] for k in ("measure", "current_minor", "comparison_minor", "explained_by")} == {
        "measure": "closing",
        "current_minor": 6_000,
        "comparison_minor": 1_000,
        "explained_by": PREPARER,
    }
    line = next(x for x in flux(client)["lines"] if x["account"] == "1130")
    assert (line["explanation"]["explanation"], line["explanation"]["current"]) == (
        "a seller's consignment arrived",
        True,
    )
    ready.load("ledger.gl_rollup_lines", [*ROWS, rollup(PERIOD, "1130", "DR", 1)])  # a late posting
    after = flux(client)
    line = next(x for x in after["lines"] if x["account"] == "1130")
    assert (line["current_minor"], line["explanation"]["current"]) == (6_001, False)
    assert "1130" in after["unexplained"]
    nothing = explain(client, "9999")
    assert (nothing.status_code, nothing.json()["title"]) == (409, "The account has no flux line")
    assert explain(client, "1130", user=AUDITOR).status_code == 403
    assert explain(client, "1130", user=APPROVER_1).status_code == 403  # a reviewer, not a preparer


def test_a_review_needs_every_line_explained_by_others_and_binds_to_them(
    client: TestClient, engine: Engine, ready: ReferenceBackend
) -> None:
    early = review(client, APPROVER_1)
    assert (early.status_code, early.json()["title"]) == (409, "Every line over its threshold needs an explanation")
    for account in ("1120", "1130", "5020"):
        assert explain(client, account).status_code == 201
    assert explain(client, "4020", user=CONTROLLER, text="the buyer-fee pilot started").status_code == 201
    own = review(client, CONTROLLER)
    assert (own.status_code, own.json()["title"]) == (409, "A reviewer can't review their own explanations")
    assert review(client, PREPARER).status_code == 403  # no close.review
    signed = review(client, APPROVER_1)
    assert signed.status_code == 201, signed.text
    out = flux(client)
    assert (out["review"]["reviewed_by"], out["review"]["current"]) == (APPROVER_1, True)
    assert signed.json()["flux_hash"] == out["flux_hash"]
    # The flux_reviewed check passes while the review is current.
    checklist = CloseChecklist(
        checklist_id="test_flux_close",
        name="Flux only",
        tasks=(CloseTask(task_id="flux", name="Flux", due_day=4, check="flux_reviewed"),),
    )
    activate(engine, ConfigKind.CLOSE_CHECKLIST, checklist.checklist_id, checklist)
    made = client.post("/api/v1/close/checklists", json={"entity_id": "US01", "period": PERIOD}, headers=h(CONTROLLER))
    assert made.status_code == 201, made.text
    task = f"/api/v1/close/tasks/US01/{PERIOD}/flux:check"
    passed = client.post(task, headers=h(PREPARER)).json()
    assert (passed["passed"], passed["evidence"]["review_id"]) == (True, signed.json()["review_id"])
    # A later explanation changes what was reviewed, as does a change in the GL.
    assert explain(client, "1130", text="a consignment, and a correction").status_code == 201
    assert flux(client)["review"]["current"] is False
    failed = client.post(task, headers=h(PREPARER)).json()
    assert (failed["passed"], failed["status"]) == (False, "open")
