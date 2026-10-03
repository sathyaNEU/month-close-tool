"""Deterministic 128-bit identifiers, computed identically in Python and in Spark.

Every engine row (items, groups, journal lines, ...) gets an ID derived from its natural key, so a
rerun of the same inputs produces the same IDs and writes stay idempotent.

Encoding (length-prefixed, so NULLs and separator characters can never collide)::

    H(ns, p1, ..., pn) = first 32 hex chars of sha256(utf8(ns + US + US.join(enc(p1), ..., enc(pn))))

    enc(None) = "-1:"
    enc(v)    = f"{len(s)}:{s}"   where s = canonical_part(v)

``US`` is the ASCII unit separator (0x1F). ``len`` counts Unicode code points, which is what Spark's
``length()`` returns for strings, so :mod:`close_engine.sparkfns.ids` feeds SHA-256 the exact same
bytes. 128 bits keeps the collision probability negligible even at 10^10+ rows (a 64-bit ID would
expect a few collisions at that scale).

Only types whose string form is identical in Python and Spark SQL are allowed as parts: str, int,
bool and date. Timestamps, floats and decimals are rejected on purpose (their string forms differ
between the two runtimes); use a date, an epoch-microsecond int, or minor-unit ints instead.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Final

US: Final = "\x1f"
"""ASCII unit separator used between encoded parts."""

ID_HEX_LEN: Final = 32
"""Hex characters kept from the SHA-256 digest (32 hex chars = 128 bits)."""

NULL_ENCODING: Final = "-1:"

_NAMESPACE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,15}$")

IdPart = str | int | bool | date | None
"""Allowed Python types for an ID part (``datetime`` is excluded even though it subclasses date)."""


class IdPartTypeError(TypeError):
    """Raised when a value of an unsupported type is used as an ID part."""


def validate_namespace(ns: str) -> str:
    """Return ``ns`` if it is a valid ID namespace (lowercase, 2-16 chars), else raise ValueError."""
    if not _NAMESPACE_RE.fullmatch(ns):
        raise ValueError(f"invalid ID namespace {ns!r}: expected ^[a-z][a-z0-9_]{{1,15}}$")
    return ns


def canonical_part(value: IdPart) -> str | None:
    """Return the canonical string form of an ID part, or None for NULL.

    The forms match Spark's ``CAST(x AS STRING)`` for the allowed types.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # must precede int: bool is a subclass of int
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        raise IdPartTypeError(
            "timestamps are not allowed in hash IDs (string forms differ between Python and Spark); "
            "use a date or an epoch-microsecond int"
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise IdPartTypeError(f"unsupported ID part type {type(value).__name__!r}; allowed: str, int, bool, date")


def encode_part(value: IdPart) -> str:
    """Length-prefix encode a single part (``-1:`` for NULL)."""
    s = canonical_part(value)
    return NULL_ENCODING if s is None else f"{len(s)}:{s}"


def id_payload(ns: str, *parts: IdPart) -> str:
    """Return the exact string that is hashed for ``hash_id(ns, *parts)`` (useful for debugging)."""
    validate_namespace(ns)
    return ns + US + US.join(encode_part(p) for p in parts)


def hash_id(ns: str, *parts: IdPart) -> str:
    """Return the 32-hex-character (128-bit) deterministic ID for ``parts`` in namespace ``ns``."""
    digest = hashlib.sha256(id_payload(ns, *parts).encode("utf-8")).hexdigest()
    return digest[:ID_HEX_LEN]
