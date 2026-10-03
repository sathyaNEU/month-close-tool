"""Journaling configuration through the stack (Gate 6): the dev users take a SAMPLE chart of accounts and an
account mapping from draft to active, and the audit chain verifies.

The stack's database outlives a run and only one chart may be active, so an active sample chart from an earlier
run is reused, and the mapping is named uniquely per run.
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


def _sample(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((SAMPLES / name).read_text(encoding="utf-8"))["body"]
    return body


def _post(user: User, url: str, body: Any = None, etag: str | None = None) -> Any:
    headers = {"Idempotency-Key": f"e2e-{uuid.uuid4()}"} | ({"If-Match": etag} if etag else {})
    return user.http.post(url, json=body, headers=headers)


def _to_active(author: User, approver: User, resource: str, body: dict[str, Any]) -> dict[str, Any]:
    r = _post(author, f"/api/v1/{resource}", body)
    assert r.status_code == 201, r.text
    v: dict[str, Any] = r.json()
    for verb, who in (("submit", author), ("approve", approver), ("activate", approver)):
        r = _post(who, f"/api/v1/{resource}/{v['config_id']}/versions/{v['version']}:{verb}", etag=v["etag"])
        assert r.status_code == 200, r.text
        v = r.json()
    return v


def test_journaling_configuration_goes_live_through_the_stack() -> None:
    alice, dave = User("alice"), User("dave")
    charts = alice.http.get("/api/v1/accounts").json()["items"]
    if not any(c["status"] == "active" for c in charts):
        assert _to_active(alice, dave, "accounts", _sample("01-accounts-sample_chart.json"))["status"] == "active"
    mapping = _sample("04-account-mappings-sample_psp_receivable_account.json")
    mapping["spec"] = mapping["spec"] | {"mapping_id": f"e2e_mapping_{uuid.uuid4().hex[:8]}"}
    active = _to_active(alice, dave, "account-mappings", mapping)
    assert active["status"] == "active"
    # Retired again, so the active journaling configuration is the same on every run: the stand-in's ledger is
    # pinned to the configuration it was first written with (Stage 8a).
    retire = f"/api/v1/account-mappings/{active['config_id']}/versions/{active['version']}:retire"
    retired = _post(dave, retire, {"effective_to": "2027-01-01"}, active["etag"])
    assert retired.status_code == 200, retired.text
    verdict = json.loads(compose("exec", "-T", "app", "python", "-m", "close_app.audit", "verify").stdout)
    assert verdict["ok"], verdict
