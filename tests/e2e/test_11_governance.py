"""Governance through the stack (Gate 4): maker-checker configuration changes by the dev users, entity
visibility, the separation-of-duties block, and the audit chain verified inside the app container.

The stack's database outlives a run, so each run names its configurations uniquely.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from stacklib import User, compose

from close_contracts.default_sources import BY_ID as SOURCES
from close_contracts.templates import BY_ID as TEMPLATES

pytestmark = pytest.mark.stack


def _post(user: User, url: str, body: Any = None, etag: str | None = None) -> Any:
    headers = {"Idempotency-Key": f"e2e-{uuid.uuid4()}"} | ({"If-Match": etag} if etag else {})
    return user.http.post(url, json=body, headers=headers)


def _advance(user: User, resource: str, v: dict[str, Any], verb: str) -> dict[str, Any]:
    r = _post(user, f"/api/v1/{resource}/{v['config_id']}/versions/{v['version']}:{verb}", etag=v["etag"])
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def test_maker_checker_through_the_stack_and_the_chain_verifies() -> None:
    alice, bob, carol, dave = User("alice"), User("bob"), User("carol"), User("dave")
    suffix = uuid.uuid4().hex[:8]
    renamed = {}
    for source_id in ("orders", "stripe_balance_transactions"):
        spec = SOURCES[source_id].model_dump(mode="json") | {"source_id": f"{source_id[:12]}_{suffix}"}
        renamed[source_id] = spec["source_id"]
        v = _post(alice, "/api/v1/data-sources", {"effective_from": "2026-10-01", "spec": spec}).json()
        v = _advance(alice, "data-sources", v, "submit")
        v = _advance(dave, "data-sources", v, "approve")
        assert _advance(dave, "data-sources", v, "activate")["status"] == "active"

    ms = TEMPLATES["t1_orders_stripe"].model_dump(mode="json")
    ms |= {"match_set_id": f"t1_{suffix}", "entity_ids": ["US01"]}
    ms["sources"] = [b | {"source_id": renamed[b["source_id"]]} for b in ms["sources"]]
    v = _post(alice, "/api/v1/match-sets", {"effective_from": "2026-10-01", "spec": ms}).json()
    v = _advance(alice, "match-sets", v, "submit")
    own = _post(alice, f"/api/v1/match-sets/{v['config_id']}/versions/1:approve", etag=v["etag"])
    assert own.status_code == 403  # alice is not an approver (SoD itself is proven in the app suite)
    v = _advance(dave, "match-sets", v, "approve")
    assert _advance(dave, "match-sets", v, "activate")["status"] == "active"

    url = f"/api/v1/match-sets/t1_{suffix}/versions/1"
    assert alice.http.get(url).status_code == 200
    assert bob.http.get(url).status_code == 404  # EU01 only
    assert carol.http.get(url).status_code == 404  # no entities

    verdict = json.loads(compose("exec", "-T", "app", "python", "-m", "close_app.audit", "verify").stdout)
    assert verdict["ok"], verdict
    assert verdict["events"] >= 10
