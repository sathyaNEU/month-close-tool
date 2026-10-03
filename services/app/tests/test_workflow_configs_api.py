"""The journal workflow's configuration kinds through the maker-checker API (Gate 7a): entities, journal templates,
the approval matrix and recurring journals, with their activation, singleton and retirement rules."""

from __future__ import annotations

from typing import Any

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from test_journaling_configs_api import APPROVER, AUTHOR, activate, create, samples, step, to_approved

from close_contracts.samples.journal_workflow import SAMPLE_RECURRING

pytestmark = pytest.mark.pg
EFFECTIVE = "2026-06-01"


@pytest.fixture
def people(engine: Engine) -> None:
    for scope in ("GLOBAL", "US01", "EU01"):
        assign(engine, AUTHOR, "config_author", scope)
        assign(engine, APPROVER, "config_approver", scope)
    assign(engine, "1001", "journal_preparer", "US01")


def one(resource: str, config_id: str) -> dict[str, Any]:
    ids = ("entity_set_id", "matrix_id", "template_id", "recurring_id", "chart_id", "dimension_set_id")
    return next(b for b in samples(resource) if any(b["spec"].get(k) == config_id for k in ids))


def refused(client: TestClient, resource: str, body: dict[str, Any]) -> Any:
    r = step(client, resource, to_approved(client, resource, body), "activate", APPROVER)
    assert r.status_code == 409, r.text
    return r.json()


def base(client: TestClient) -> None:
    for resource in ("accounts", "dimensions", "entities", "approval-matrices"):
        activate(client, resource, samples(resource)[0])


def retire(client: TestClient, resource: str, v: dict[str, Any]) -> Any:
    return step(client, resource, v, "retire", APPROVER, effective_to="2027-01-01")


def test_the_matrix_needs_every_functional_currency_and_one_is_active(client: TestClient, people: None) -> None:
    activate(client, "entities", samples("entities")[0])
    matrix = samples("approval-matrices")[0]
    no_pounds = {
        "effective_from": EFFECTIVE,
        "spec": matrix["spec"]
        | {
            "matrix_id": "matrix_without_pounds",
            "thresholds": [t for t in matrix["spec"]["thresholds"] if t["currency"] != "GBP"],
        },
    }
    problem = refused(client, "approval-matrices", no_pounds)
    assert [(i["code"], i["message"]) for i in problem["issues"]] == [("missing_thresholds", "no thresholds for GBP")]
    activate(client, "approval-matrices", matrix)
    other = {"effective_from": EFFECTIVE, "spec": matrix["spec"] | {"matrix_id": "other_matrix"}}
    assert refused(client, "approval-matrices", other)["title"] == "Another one is active"


def test_templates_need_the_chart_and_its_accounts(client: TestClient, people: None) -> None:
    standard = one("journal-templates", "sample_standard_journal")
    early = standard | {"spec": standard["spec"] | {"template_id": "template_before_the_chart"}}
    assert refused(client, "journal-templates", early)["issues"][0]["code"] == "no_active_chart"
    activate(client, "accounts", samples("accounts")[0])
    listed = standard | {"spec": standard["spec"] | {"allowed_accounts": {"codes": ["0000"]}}}
    r = create(client, "journal-templates", listed)
    assert (r.status_code, r.json()["issues"][0]["code"]) == (422, "unknown_account")


def test_a_recurring_journal_needs_its_references_a_preparer_and_permission_to_bypass(
    client: TestClient, engine: Engine, people: None
) -> None:
    base(client)
    activate(client, "journal-templates", one("journal-templates", "sample_standard_journal"))
    monthly = one("recurring-journals", "sample_reserve_top_up")
    stranger = monthly | {"spec": monthly["spec"] | {"preparer": "9999", "recurring_id": "sample_strangers_top_up"}}
    issues = {i["code"] for i in refused(client, "recurring-journals", stranger)["issues"]}
    assert issues == {"preparer_lacks_role"}
    with engine.begin() as tx:  # a matrix that doesn't let recurring journals skip approval
        tx.execute(
            text(
                "UPDATE config_versions SET spec = jsonb_set(spec, '{bypass,recurring}', 'false') "
                "WHERE kind = 'approval_matrix'"
            )
        )
    assert {i["code"] for i in refused(client, "recurring-journals", monthly)["issues"]} == {"bypass_not_allowed"}
    manual = monthly | {"spec": monthly["spec"] | {"auto_approve": False, "recurring_id": "sample_manual_top_up"}}
    assert activate(client, "recurring-journals", manual)["entity_ids"] == ["US01"]


def test_recurring_journals_belong_to_their_entity(client: TestClient, engine: Engine, people: None) -> None:
    assign(engine, "8001", "config_author", "EU01")  # an author in another entity only
    base(client)
    activate(client, "journal-templates", one("journal-templates", "sample_standard_journal"))
    headers = {"X-Forwarded-User": "8001", "X-Forwarded-Email": "e@x", "Idempotency-Key": "k-us01-recurring-1"}
    monthly = one("recurring-journals", "sample_reserve_top_up")
    assert client.post("/api/v1/recurring-journals", json=monthly, headers=headers).status_code == 403
    activate(client, "recurring-journals", monthly)
    listed = client.get("/api/v1/recurring-journals", headers=headers | {"Idempotency-Key": ""}).json()["items"]
    assert listed == []  # a US01 recurring journal is not visible from EU01


def test_nothing_in_use_can_be_retired(client: TestClient, engine: Engine, people: None) -> None:
    base(client)
    template = activate(client, "journal-templates", one("journal-templates", "sample_standard_journal"))
    recurring = activate(client, "recurring-journals", one("recurring-journals", "sample_reserve_top_up"))
    in_use = retire(client, "journal-templates", template)
    assert (in_use.status_code, in_use.json()["detail"].split(";")[0]) == (
        409,
        "active configurations use it: sample_reserve_top_up",
    )
    matrix = client.get(
        "/api/v1/approval-matrices/sample_approval_matrix/versions/1",
        headers={"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x"},
    ).json()
    assert retire(client, "approval-matrices", matrix).status_code == 409  # templates route through it
    chart = client.get(
        "/api/v1/accounts/sample_chart/versions/1", headers={"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x"}
    ).json()
    assert retire(client, "accounts", chart).status_code == 409
    assert retire(client, "recurring-journals", recurring).status_code == 200
    assert retire(client, "journal-templates", template).status_code == 200
    assert retire(client, "approval-matrices", matrix).status_code == 200


def test_a_chart_version_keeps_what_templates_and_recurring_journals_use(client: TestClient, people: None) -> None:
    base(client)
    activate(client, "journal-templates", one("journal-templates", "sample_standard_journal"))
    activate(client, "recurring-journals", one("recurring-journals", "sample_reserve_top_up"))
    chart = samples("accounts")[0]
    closed = [a | {"status": "inactive"} if a["code"] == "6040" else a for a in chart["spec"]["accounts"]]
    r = client.post(
        "/api/v1/accounts/sample_chart/versions",
        json={"effective_from": "2026-12-01", "spec": chart["spec"] | {"accounts": closed}},
        headers={"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x", "Idempotency-Key": "k-chart-v2-6040"},
    )
    assert r.status_code == 201, r.text
    v = step(client, "accounts", r.json(), "submit", AUTHOR).json()
    v = step(client, "accounts", v, "approve", APPROVER).json()
    problem = step(client, "accounts", v, "activate", APPROVER).json()
    assert [(i["code"], i["message"]) for i in problem["issues"]] == [
        ("account_in_use", "account 6040 is used by sample_reserve_top_up")
    ]


def test_the_entity_set_keeps_entities_with_journals(client: TestClient, engine: Engine, people: None) -> None:
    entities = activate(client, "entities", samples("entities")[0])
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO accounting_periods (entity_id, period, starts_on, ends_on, status, opened_by) "
                "VALUES ('UK01', '2026-09', '2026-09-01', '2026-09-30', 'open', 't')"
            )
        )
    assert retire(client, "entities", entities).status_code == 409
    spec = samples("entities")[0]["spec"]
    fewer = {
        "effective_from": "2026-12-01",
        "spec": spec | {"entities": [e for e in spec["entities"] if e["entity_id"] != "UK01"]},
    }
    r = client.post(
        "/api/v1/entities/sample_entities/versions",
        json=fewer,
        headers={"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x", "Idempotency-Key": "k-entities-v2"},
    )
    v = step(
        client, "entities", step(client, "entities", r.json(), "submit", AUTHOR).json(), "approve", APPROVER
    ).json()
    problem = step(client, "entities", v, "activate", APPROVER).json()
    assert [i["code"] for i in problem["issues"]] == ["entity_in_use"]
    assert SAMPLE_RECURRING[0].preparer == "1001"
