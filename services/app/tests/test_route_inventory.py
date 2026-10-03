"""The route inventory (Gate 4): nothing is reachable by accident.

It reads the published OpenAPI document, which lists every operation the app serves:

- Every operation declares its access (``x-close-access``): ``public``, ``authenticated`` or an action.
- Every POST requires ``Idempotency-Key``.
- Every ``authenticated`` operation is reviewed here for how it limits what it returns to the caller's
  entities. This replaces the roadmap's query-template registry (ADR-0009: reads are typed query specs,
  and the query engine always adds the caller's entity filter).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from close_app.authz import ACCESS_KEY, Action
from close_app.main import create_app

# Each authenticated read, and why it cannot leak another entity's data.
AUTHENTICATED_READS = {
    ("GET", "/api/v1/whoami"): "the caller's own identity and assignments",
    ("GET", "/api/v1/roles"): "the role catalog; no entity data",
    ("GET", "/api/v1/smoke/runs/{run_id}"): "the result is cut to the caller's entities",
    ("GET", "/api/v1/smoke/runs/{run_id}/rows"): "a QuerySpec; the query engine adds the entity filter",
    ("GET", "/api/v1/smoke/runs/{run_id}/totals"): "a QuerySpec; the query engine adds the entity filter",
    ("GET", "/api/v1/delegations"): "only the caller's own delegations, given or received",
    ("GET", "/api/v1/notifications"): "only the caller's own notifications",
    ("POST", "/api/v1/notifications/{notification_id}:read"): "only the caller's own notification; another's is a 404",
}


def operations() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in create_app().openapi()["paths"].items():
        for method, operation in item.items():
            yield method.upper(), path, operation


def test_the_inventory_sees_every_router() -> None:
    paths = {path for _, path, _ in operations()}
    for expected in ("/healthz", "/api/v1/whoami", "/api/v1/match-sets", "/api/v1/role-assignments"):
        assert expected in paths


def test_every_operation_declares_its_access() -> None:
    known = {"public", "authenticated"} | {a.value for a in Action}
    undeclared = [(m, p) for m, p, op in operations() if op.get(ACCESS_KEY) not in known]
    assert undeclared == []


def test_every_post_requires_an_idempotency_key() -> None:
    def has_key(op: dict[str, Any]) -> bool:
        return any(q["in"] == "header" and q["name"].lower() == "idempotency-key" for q in op.get("parameters", []))

    assert [p for m, p, op in operations() if m == "POST" and not has_key(op)] == []


def test_every_authenticated_operation_is_reviewed_for_entity_scoping() -> None:
    authenticated = {(m, p) for m, p, op in operations() if op.get(ACCESS_KEY) == "authenticated"}
    assert authenticated == set(AUTHENTICATED_READS)


def test_public_operations_are_only_health_and_readiness() -> None:
    assert {p for _, p, op in operations() if op.get(ACCESS_KEY) == "public"} == {"/healthz", "/readyz"}
