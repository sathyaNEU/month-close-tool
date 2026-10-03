"""The journal workflow through the stack (Gate 7a): the dev users make a SAMPLE journal setup active if it isn't,
open September, and take a two-level journal from draft to approved. alice prepares it, dave approves level 1 and
erin level 2. The audit chain verifies afterwards.

The stack's database outlives a run, so configuration and periods already active from an earlier run are reused.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from stacklib import User, compose

pytestmark = pytest.mark.stack
SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "samples" / "journaling"
PERIOD = "2026-09"


def _sample(prefix: str) -> dict[str, Any]:
    [path] = SAMPLES.glob(f"*-{prefix}.json")
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["body"]
    return body


def _post(user: User, url: str, body: Any = None, etag: str | None = None) -> Any:
    headers = {"Idempotency-Key": f"e2e-{uuid.uuid4()}"} | ({"If-Match": etag} if etag else {})
    return user.http.post(url, json=body, headers=headers)


def _active(user: User, resource: str, config_id: str | None = None) -> bool:
    items = user.http.get(f"/api/v1/{resource}").json()["items"]
    return any(i["status"] == "active" and (config_id is None or i["config_id"] == config_id) for i in items)


def _make_active(author: User, approver: User, resource: str, body: dict[str, Any]) -> None:
    r = _post(author, f"/api/v1/{resource}", body)
    assert r.status_code == 201, r.text
    v: dict[str, Any] = r.json()
    for verb, who in (("submit", author), ("approve", approver), ("activate", approver)):
        r = _post(who, f"/api/v1/{resource}/{v['config_id']}/versions/{v['version']}:{verb}", etag=v["etag"])
        assert r.status_code == 200, r.text
        v = r.json()


def test_a_journal_goes_through_two_approval_levels_on_the_stack() -> None:
    alice, bob, dave, erin = User("alice"), User("bob"), User("dave"), User("erin")
    for resource, prefix, config_id in (
        ("accounts", "accounts-sample_chart", None),
        ("entities", "entities-sample_entities", None),
        ("approval-matrices", "approval-matrices-sample_approval_matrix", None),
        ("journal-templates", "journal-templates-sample_standard_journal", "sample_standard_journal"),
    ):
        if not _active(alice, resource, config_id):
            _make_active(alice, dave, resource, _sample(prefix))
    period = dave.http.get(f"/api/v1/periods/US01/{PERIOD}")
    if period.status_code == 404:
        assert _post(dave, "/api/v1/periods", {"entity_id": "US01", "period": PERIOD}).status_code == 201
    else:
        assert period.json()["status"] == "open", period.json()
    body = {
        "entity_id": "US01",
        "template_id": "sample_standard_journal",
        "accounting_date": f"{PERIOD}-15",
        "description": f"Carrier accrual true-up, stack run {uuid.uuid4().hex[:8]}",
        "lines": [
            {"account": "5020", "side": "DR", "amount_minor": 2_000_000},
            {"account": "2410", "side": "CR", "amount_minor": 2_000_000},
        ],
    }
    created = _post(alice, "/api/v1/journals", body)
    assert created.status_code == 201, created.text
    j = created.json()
    url = f"/api/v1/journals/{j['journal_id']}"
    j = _post(alice, url + ":submit", etag=j["etag"]).json()
    assert (j["status"], j["required_levels"]) == ("pending_approval", 2)
    assert _post(alice, url + ":approve", etag=j["etag"]).status_code == 403  # alice holds no approver role
    j = _post(dave, url + ":approve", etag=j["etag"]).json()
    assert (j["status"], j["current_level"]) == ("pending_approval", 2)
    assert _post(dave, url + ":approve", etag=j["etag"]).status_code == 403  # dave approves level 1 only
    j = _post(erin, url + ":approve", etag=j["etag"]).json()
    assert j["status"] == "approved"
    assert [(a["level"], a["approver"]) for a in j["approvals"]] == [(1, "1004"), (2, "1005")]
    assert bob.http.get(url).status_code == 404  # bob sees EU01 only
    verdict = json.loads(compose("exec", "-T", "app", "python", "-m", "close_app.audit", "verify").stdout)
    assert verdict["ok"], verdict
