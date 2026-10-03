"""Journaling configuration through the maker-checker API (Gate 6): every sample goes from draft to active,
and activation refuses missing, inactive or conflicting references."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from apptestkit import assign, journal_samples
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from close_app import audit

pytestmark = pytest.mark.pg

AUTHOR, APPROVER = "6001", "6002"


@pytest.fixture
def people(engine: Engine) -> None:
    for scope in ("GLOBAL", "US01", "EU01"):  # recurring journals belong to their entity
        assign(engine, AUTHOR, "config_author", scope)
        assign(engine, APPROVER, "config_approver", scope)
    assign(engine, "1001", "journal_preparer", "US01")  # the sample recurring journals' preparers
    assign(engine, "1004", "journal_preparer", "EU01")
    assign(engine, "1004", "controller", "US01")  # Stage 8b: the sample checklist's preparers and reviewers
    assign(engine, "1005", "journal_approver_l2", "US01")


def h(user: str, if_match: str | None = None) -> dict[str, str]:
    headers = {"X-Forwarded-User": user, "X-Forwarded-Email": f"{user}@x", "Idempotency-Key": f"k-{uuid.uuid4()}"}
    return headers | ({"If-Match": if_match} if if_match else {})


def create(client: TestClient, resource: str, body: dict[str, Any]) -> Any:
    return client.post(f"/api/v1/{resource}", json=body, headers=h(AUTHOR))


def step(client: TestClient, resource: str, v: dict[str, Any], verb: str, user: str, **body: Any) -> Any:
    url = f"/api/v1/{resource}/{v['config_id']}/versions/{v['version']}:{verb}"
    return client.post(url, json=body or None, headers=h(user, v["etag"]))


def to_approved(client: TestClient, resource: str, body: dict[str, Any]) -> dict[str, Any]:
    r = create(client, resource, body)
    assert r.status_code == 201, r.text
    v = step(client, resource, r.json(), "submit", AUTHOR).json()
    v = step(client, resource, v, "approve", APPROVER).json()
    assert v["status"] == "approved", v
    return v  # type: ignore[no-any-return]


def activate(client: TestClient, resource: str, body: dict[str, Any]) -> dict[str, Any]:
    r = step(client, resource, to_approved(client, resource, body), "activate", APPROVER)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def samples(resource: str) -> list[dict[str, Any]]:
    return [body for res, body in journal_samples() if res == resource]


def test_every_sample_goes_from_draft_to_active(client: TestClient, engine: Engine, people: None) -> None:
    published = journal_samples()
    assert len(published) == 47  # Stage 7c added the foreign-currency template, 8b the checklist and flux rules
    for resource, body in published:
        assert activate(client, resource, body)["status"] == "active"
    with engine.connect() as conn:
        kinds = dict(
            conn.execute(text("SELECT kind, count(*) FROM config_versions WHERE status = 'active' GROUP BY kind")).all()
        )
        assert audit.verify(conn).ok
    assert kinds == {
        "chart_of_accounts": 1,
        "dimension_set": 1,
        "account_mapping": 5,
        "event_type": 13,
        "auto_journal_definition": 16,
        "accounting_policy": 1,
        "entity_set": 1,
        "approval_matrix": 1,
        "journal_template": 4,  # Stage 7c: sample_fx_journal
        "recurring_journal": 2,
        "close_checklist": 1,  # Stage 8b
        "flux_rules": 1,
    }


def test_a_definition_waits_for_active_references(client: TestClient, people: None) -> None:
    for resource in ("accounts", "dimensions", "account-mappings"):
        for body in samples(resource):
            activate(client, resource, body)
    capture_type = next(b for b in samples("event-types") if b["spec"]["event_type"] == "ORDER_CAPTURED")
    create(client, "event-types", capture_type)  # a draft only
    definition = next(
        b for b in samples("auto-journal-definitions") if b["spec"]["definition_id"] == "sample_order_capture"
    )
    r = step(
        client,
        "auto-journal-definitions",
        to_approved(client, "auto-journal-definitions", definition),
        "activate",
        APPROVER,
    )
    assert r.status_code == 409
    assert "unknown_event_type" in {i["code"] for i in r.json()["issues"]}


def test_a_mapping_needs_an_active_chart(client: TestClient, people: None) -> None:
    create(client, "accounts", samples("accounts")[0])  # a draft chart only
    r = step(
        client,
        "account-mappings",
        to_approved(client, "account-mappings", samples("account-mappings")[0]),
        "activate",
        APPROVER,
    )
    assert r.status_code == 409
    assert {i["code"] for i in r.json()["issues"]} == {"no_active_chart"}


def test_a_chart_version_cannot_retire_an_account_in_use(client: TestClient, people: None) -> None:
    for resource, body in journal_samples():
        activate(client, resource, body)
    chart = samples("accounts")[0]
    spec = chart["spec"] | {
        "accounts": [a | {"status": "inactive"} if a["code"] == "4010" else a for a in chart["spec"]["accounts"]]
    }
    r = client.post(
        "/api/v1/accounts/sample_chart/versions", json={"effective_from": "2026-12-01", "spec": spec}, headers=h(AUTHOR)
    )
    assert r.status_code == 201
    v = step(client, "accounts", r.json(), "submit", AUTHOR).json()
    v = step(client, "accounts", v, "approve", APPROVER).json()
    refused = step(client, "accounts", v, "activate", APPROVER)
    assert refused.status_code == 409
    assert "sample_revenue_at_trigger" in refused.json()["issues"][0]["message"]


def test_only_one_chart_is_active(client: TestClient, people: None) -> None:
    chart = samples("accounts")[0]
    activate(client, "accounts", chart)
    other = {"effective_from": chart["effective_from"], "spec": chart["spec"] | {"chart_id": "other_chart"}}
    assert step(client, "accounts", to_approved(client, "accounts", other), "activate", APPROVER).status_code == 409


def test_event_type_codes_are_unique_among_active_types(client: TestClient, people: None) -> None:
    body = samples("event-types")[0]
    activate(client, "event-types", body)
    twin = {"effective_from": body["effective_from"], "spec": body["spec"] | {"type_id": "twin_type"}}
    r = step(client, "event-types", to_approved(client, "event-types", twin), "activate", APPROVER)
    assert r.status_code == 409
    assert {i["code"] for i in r.json()["issues"]} == {"duplicate_code"}


def test_a_definition_naming_an_unknown_mapping_is_refused_on_save(client: TestClient, people: None) -> None:
    for resource in ("accounts", "dimensions"):
        activate(client, resource, samples(resource)[0])
    for body in samples("event-types"):
        create(client, "event-types", body)
    body = next(b for b in samples("auto-journal-definitions") if b["spec"]["definition_id"] == "sample_order_capture")
    r = create(client, "auto-journal-definitions", body)
    assert r.status_code == 422
    assert "unknown_mapping" in {i["code"] for i in r.json()["issues"]}


def _activate_all(client: TestClient) -> dict[tuple[str, str], dict[str, Any]]:
    active = {}
    for resource, body in journal_samples():
        v = activate(client, resource, body)
        active[(resource, v["config_id"])] = v
    return active


def test_nothing_an_active_configuration_uses_can_be_retired(client: TestClient, people: None) -> None:
    active = _activate_all(client)
    for key, users in (
        (("accounts", "sample_chart"), "sample_carrier_charge, sample_clawback"),  # every definition and mapping
        (("dimensions", "sample_dimensions"), "sample_carrier_charge, sample_clawback"),  # every definition
        (("programs", "accounting_policy"), "sample_carrier_charge, sample_clawback"),  # every definition
        (("event-types", "sample_tax_remitted"), "sample_tax_remitted;"),
        (("account-mappings", "sample_carrier_charge_account"), "sample_carrier_charge;"),
    ):
        refused = step(client, key[0], active[key], "retire", APPROVER, effective_to="2027-01-01")
        assert refused.status_code == 409, (key, refused.text)
        assert refused.json()["detail"].startswith(f"active configurations use it: {users}"), refused.json()
    # Retiring the user first frees what it used.
    definition = active[("auto-journal-definitions", "sample_tax_remitted")]
    assert step(
        client, "auto-journal-definitions", definition, "retire", APPROVER, effective_to="2027-01-01"
    ).is_success
    event_type = active[("event-types", "sample_tax_remitted")]
    retired = step(client, "event-types", event_type, "retire", APPROVER, effective_to="2027-01-01")
    assert retired.json()["status"] == "retired"


def _new_version(client: TestClient, resource: str, config_id: str, spec: dict[str, Any]) -> Any:
    r = client.post(
        f"/api/v1/{resource}/{config_id}/versions",
        json={"effective_from": "2026-12-01", "spec": spec},
        headers=h(AUTHOR),
    )
    assert r.status_code == 201, r.text
    v = step(client, resource, r.json(), "submit", AUTHOR).json()
    v = step(client, resource, v, "approve", APPROVER).json()
    return step(client, resource, v, "activate", APPROVER)


def test_a_new_version_cannot_break_an_active_definition(client: TestClient, people: None) -> None:
    _activate_all(client)
    captured = next(b for b in samples("event-types") if b["spec"]["type_id"] == "sample_order_captured")["spec"]
    refused = _new_version(client, "event-types", "sample_order_captured", captured | {"event_type": "ORDER_TAKEN"})
    assert refused.status_code == 409
    assert {(i["code"], i["message"].split("; ")[-1]) for i in refused.json()["issues"]} == {
        ("unknown_event_type", "active definition sample_order_capture would break"),
        ("unknown_event_type", "active definition sample_revenue_at_trigger would break"),
    }
    dims = samples("dimensions")[0]["spec"]
    no_program = dims | {"dimensions": [d for d in dims["dimensions"] if d["code"] != "program"]}
    refused = _new_version(client, "dimensions", "sample_dimensions", no_program)
    assert refused.status_code == 409
    assert {i["code"] for i in refused.json()["issues"]} == {"unknown_dimension"}
    # A change that keeps every active definition valid goes live and supersedes the active version.
    remitted = next(b for b in samples("event-types") if b["spec"]["type_id"] == "sample_tax_remitted")["spec"]
    described = remitted | {"description": "Sales tax paid to a tax authority"}
    assert _new_version(client, "event-types", "sample_tax_remitted", described).json()["status"] == "active"


def test_the_policy_is_one_configuration(client: TestClient, people: None) -> None:
    v = activate(client, "programs", samples("programs")[0])
    assert (v["config_id"], v["kind"]) == ("accounting_policy", "accounting_policy")
