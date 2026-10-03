"""Page cursors for the read path: opaque, signed, short-lived, and bound to one spec and one caller.

A cursor carries the table version the first page was read at (so every page reads the same
snapshot), the sort values of the last row returned (the next page starts strictly after them), the
fingerprint of the spec, the caller's user id and an expiry. It is signed with HMAC-SHA256, so a
client can neither forge one nor reuse it for another query or another user. It deliberately does
not carry the entity filter: that is recomputed from the caller's current grants on every page, so a
revoked grant takes effect in the middle of paging.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from close_contracts.query import QueryError, Scalar, typed, untyped

DEFAULT_TTL_S = 15 * 60


class CursorError(QueryError):
    """A cursor that is malformed, forged, expired, or issued for another spec or caller."""


@dataclass(frozen=True)
class CursorState:
    spec: str
    principal: str
    version: int | None
    after: tuple[Scalar, ...]
    expires: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CursorCodec:
    def __init__(self, key: bytes, *, ttl_s: int = DEFAULT_TTL_S, clock: Callable[[], float] = time.time) -> None:
        if len(key) < 32:
            raise ValueError("the cursor key must be at least 32 bytes")
        self._key = key
        self._ttl_s = ttl_s
        self._clock = clock

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()

    def issue(self, *, spec: str, principal: str, version: int | None, after: tuple[Scalar, ...]) -> str:
        body = {
            "s": spec,
            "p": principal,
            "v": version,
            "a": [typed(v) for v in after],
            "e": int(self._clock()) + self._ttl_s,
        }
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return f"{_b64(payload)}.{_b64(self._sign(payload))}"

    def open(self, token: str, *, spec: str, principal: str) -> CursorState:
        try:
            payload_b64, signature_b64 = token.split(".")
            payload, signature = _unb64(payload_b64), _unb64(signature_b64)
        except (ValueError, TypeError):
            raise CursorError("malformed cursor") from None
        if not hmac.compare_digest(signature, self._sign(payload)):
            raise CursorError("invalid cursor")
        try:
            body = json.loads(payload)
            after = tuple(untyped(pair) for pair in body["a"])
            state = CursorState(
                spec=str(body["s"]),
                principal=str(body["p"]),
                version=None if body["v"] is None else int(body["v"]),
                after=tuple(v for v in after if v is not None),
                expires=int(body["e"]),
            )
        except (KeyError, TypeError, ValueError):
            raise CursorError("malformed cursor") from None
        if len(state.after) != len(after):  # sort columns are non-nullable, so a NULL here is corruption
            raise CursorError("malformed cursor")
        if state.spec != spec:
            raise CursorError("cursor belongs to a different query")
        if state.principal != principal:
            raise CursorError("cursor belongs to a different user")
        if state.expires < self._clock():
            raise CursorError("cursor expired; start again from the first page")
        return state
