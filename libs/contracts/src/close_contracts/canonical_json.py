"""Canonical JSON (RFC 8785 / JCS) for content hashing of configuration.

Config versions (match sets, pass rules, journal templates, posting rules, ...) are identified by the
SHA-256 of their canonical JSON, so the same logical content always hashes the same way no matter how
it was formatted or in which key order it arrived.

This implements the RFC 8785 rules for the value types our configuration uses:

* object members are sorted by the UTF-16 code units of their names (not by code point);
* no insignificant whitespace; strings are UTF-8 with only the escapes JSON requires;
* integers are written in plain decimal form.

Floating-point numbers are rejected rather than serialized. Money is always integer minor units,
percentages are integer basis points, and FX rates travel as decimal strings, so a float in config
is a modelling error we want to catch early. Integers outside the IEEE-754 safe range (|n| > 2^53-1)
are rejected too, because other JCS implementations would silently round them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Final

MAX_SAFE_INTEGER: Final = 2**53 - 1

JSONValue = None | bool | int | str | Sequence["JSONValue"] | Mapping[str, "JSONValue"]


class CanonicalJSONError(ValueError):
    """Raised when a value cannot be represented in canonical JSON."""


def _utf16_key(name: str) -> bytes:
    return name.encode("utf-16-be", errors="surrogatepass")


def _serialize(value: object, path: str) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalJSONError(f"{path}: integer {value} is outside the IEEE-754 safe range")
        return str(value)
    if isinstance(value, float):
        raise CanonicalJSONError(
            f"{path}: floats are not allowed in canonical config JSON; use integer minor units, "
            "integer basis points, or a decimal string"
        )
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalJSONError(f"{path}: object keys must be strings, got {type(key).__name__}")
        items = [
            json.dumps(key, ensure_ascii=False) + ":" + _serialize(value[key], f"{path}.{key}")
            for key in sorted(value, key=_utf16_key)
        ]
        return "{" + ",".join(items) + "}"
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return "[" + ",".join(_serialize(v, f"{path}[{i}]") for i, v in enumerate(value)) + "]"
    raise CanonicalJSONError(f"{path}: unsupported type {type(value).__name__}")


def canonical_json(value: JSONValue) -> bytes:
    """Serialize ``value`` to RFC 8785 canonical JSON bytes (UTF-8)."""
    return _serialize(value, "$").encode("utf-8")


def content_sha256(value: JSONValue) -> str:
    """Return the hex SHA-256 of the canonical JSON of ``value`` (the config content hash)."""
    return hashlib.sha256(canonical_json(value)).hexdigest()
