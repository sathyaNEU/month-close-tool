"""``Idempotency-Key`` for POST requests, stored in the same transaction as the request's effect.

The first request inserts the key and, before committing, the response it produced. A concurrent
duplicate's insert waits on the unique key until the first transaction commits, then finds the row
and replays the stored response. If the first request failed and rolled back, nothing was stored and
a retry simply executes again. Reusing a key for a different request body is a 422.

Every POST goes through :func:`run_once`. The key is scoped to the principal, the method and the concrete
path (ids included), so the same key on two different resources never replays the wrong response. The
lookup happens before the effect checks ``If-Match``, so a replay after a later change still returns the
stored response instead of a 412.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, Engine, text

from close_app.errors import Problem


class CanonicalJSONResponse(JSONResponse):
    """Sorted keys, so a replay (read back from jsonb, which reorders keys) is byte-identical."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: dict[str, Any]


@dataclass(frozen=True)
class IdempotencyScope:
    principal: str
    method: str
    route: str
    key: str
    request_sha256: bytes


def scope(principal: str, method: str, route: str, key: str | None, body: bytes) -> IdempotencyScope:
    if key is None:
        raise Problem(400, "Idempotency-Key header is required")
    if not 8 <= len(key) <= 128 or not key.isascii() or not key.isprintable():
        raise Problem(400, "Invalid Idempotency-Key", "use 8-128 printable ASCII characters")
    return IdempotencyScope(principal, method, route, key, hashlib.sha256(body).digest())


def begin(tx: Connection, s: IdempotencyScope) -> StoredResponse | None:
    """Returns the stored response for a replay, or None if this is the first request."""
    params = {"p": s.principal, "m": s.method, "r": s.route, "k": s.key}
    tx.execute(
        text(
            "DELETE FROM idempotency_keys WHERE principal = :p AND method = :m AND route = :r AND key = :k "
            "AND expires_at < now()"
        ),
        params,
    )
    inserted = tx.execute(
        text(
            "INSERT INTO idempotency_keys "
            "(principal, method, route, key, request_sha256, response_status, response_body) "
            "VALUES (:p, :m, :r, :k, :sha, 0, '{}'::jsonb) ON CONFLICT DO NOTHING RETURNING key"
        ),
        {**params, "sha": s.request_sha256},
    ).first()
    if inserted is not None:
        return None
    row = tx.execute(
        text(
            "SELECT request_sha256, response_status, response_body FROM idempotency_keys "
            "WHERE principal = :p AND method = :m AND route = :r AND key = :k"
        ),
        params,
    ).one()
    if bytes(row.request_sha256) != s.request_sha256:
        raise Problem(422, "Idempotency-Key reused with a different request")
    return StoredResponse(int(row.response_status), dict(row.response_body))


def complete(tx: Connection, s: IdempotencyScope, status: int, body: dict[str, Any], resource_ref: str | None) -> None:
    tx.execute(
        text(
            "UPDATE idempotency_keys SET response_status = :st, response_body = CAST(:b AS jsonb), resource_ref = :ref "
            "WHERE principal = :p AND method = :m AND route = :r AND key = :k"
        ),
        {
            "st": status,
            "b": json.dumps(body, sort_keys=True),
            "ref": resource_ref,
            "p": s.principal,
            "m": s.method,
            "r": s.route,
            "k": s.key,
        },
    )


Effect = Callable[[Connection], tuple[int, dict[str, Any], str | None]]  # (status, body, resource ref)


def _headers(body: dict[str, Any], replayed: bool) -> dict[str, str]:
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    if isinstance(body.get("etag"), str):
        headers["ETag"] = body["etag"]
    return headers


def _scope_of(request: Request, principal: str, payload: Any) -> IdempotencyScope:
    keys = request.headers.getlist("idempotency-key")
    if len(keys) > 1:
        raise Problem(400, "Invalid Idempotency-Key", "send the header once")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return scope(principal, request.method, request.url.path, keys[0] if keys else None, canonical)


def replay(engine: Engine, request: Request, principal: str, payload: Any) -> CanonicalJSONResponse | None:
    """The response a completed earlier request stored under this key, read without starting anything.

    A route that has work to do outside its transaction (an exchange-rate lookup, a workbook parse) calls this
    first, so a replay returns the stored response instead of repeating that work or failing a check the first
    request's effect has since changed. :func:`run_once` still decides for every request that isn't a replay."""
    s = _scope_of(request, principal, payload)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT request_sha256, response_status, response_body FROM idempotency_keys WHERE principal = :p "
                "AND method = :m AND route = :r AND key = :k AND response_status <> 0 AND expires_at >= now()"
            ),
            {"p": s.principal, "m": s.method, "r": s.route, "k": s.key},
        ).first()
    if row is None:
        return None
    if bytes(row.request_sha256) != s.request_sha256:
        raise Problem(422, "Idempotency-Key reused with a different request")
    body = dict(row.response_body)
    return CanonicalJSONResponse(body, status_code=int(row.response_status), headers=_headers(body, True))


def run_once(engine: Engine, request: Request, principal: str, payload: Any, effect: Effect) -> CanonicalJSONResponse:
    """Run ``effect`` at most once per idempotency key, in the transaction that stores the key and response."""
    s = _scope_of(request, principal, payload)
    with engine.begin() as tx:
        stored = begin(tx, s)
        if stored is not None:
            return CanonicalJSONResponse(stored.body, status_code=stored.status, headers=_headers(stored.body, True))
        status, body, ref = effect(tx)
        complete(tx, s, status, body, ref)
    return CanonicalJSONResponse(body, status_code=status, headers=_headers(body, False))
