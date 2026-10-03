from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from close_contracts.canonical_json import MAX_SAFE_INTEGER, CanonicalJSONError, canonical_json, content_sha256


def test_rfc8785_property_sorting_uses_utf16_code_units() -> None:
    # Example from RFC 8785 section 3.2.3: the emoji (U+1F600, UTF-16 D83D DE00) sorts before
    # U+FB33 because 0xD83D < 0xFB33, although it has the larger code point.
    value = {
        "\u20ac": "Euro Sign",
        "\r": "Carriage Return",
        "\ufb33": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "\u00f6": "Latin Small Letter O With Diaeresis",
    }
    expected = (
        '{"\\r":"Carriage Return","1":"One","\u0080":"Control","\u00f6":"Latin Small Letter O With Diaeresis",'
        '"\u20ac":"Euro Sign","\U0001f600":"Emoji: Grinning Face","\ufb33":"Hebrew Letter Dalet With Dagesh"}'
    )
    assert canonical_json(value).decode("utf-8") == expected


def test_no_whitespace_and_nested_structures() -> None:
    value = {"b": [1, {"z": None, "a": True}], "a": "x"}
    assert canonical_json(value) == b'{"a":"x","b":[1,{"a":true,"z":null}]}'


def test_string_escapes_are_minimal() -> None:
    assert canonical_json('a"b\\c\n\t\x1f\u2028') == b'"a\\"b\\\\c\\n\\t\\u001f\xe2\x80\xa8"'


@pytest.mark.parametrize("bad", [1.5, {"x": 0.1}, [1, 2.0], float("nan")])
def test_floats_are_rejected(bad: Any) -> None:
    with pytest.raises(CanonicalJSONError, match="floats are not allowed"):
        canonical_json(bad)


@pytest.mark.parametrize("n", [MAX_SAFE_INTEGER + 1, -(MAX_SAFE_INTEGER + 1), 10**20])
def test_unsafe_integers_are_rejected(n: int) -> None:
    with pytest.raises(CanonicalJSONError, match="safe range"):
        canonical_json({"n": n})


def test_safe_integer_bounds_are_accepted() -> None:
    assert canonical_json([MAX_SAFE_INTEGER, -MAX_SAFE_INTEGER, 0]) == b"[9007199254740991,-9007199254740991,0]"


@pytest.mark.parametrize("bad", [{1: "x"}, {"a": b"bytes"}, {"a": object()}])
def test_unsupported_types_are_rejected(bad: Any) -> None:
    with pytest.raises(CanonicalJSONError):
        canonical_json(bad)


def test_content_sha256_is_sha256_of_canonical_bytes() -> None:
    value = {"rule_id": "psp-bank-10", "sequence": 10}
    assert content_sha256(value) == hashlib.sha256(b'{"rule_id":"psp-bank-10","sequence":10}').hexdigest()


_json = st.recursive(
    st.none() | st.booleans() | st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER) | st.text(max_size=8),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4),
    max_leaves=20,
)


@given(_json)
def test_canonical_json_round_trips_and_is_order_independent(value: Any) -> None:
    encoded = canonical_json(value)
    assert json.loads(encoded.decode("utf-8")) == value
    if isinstance(value, dict):
        reversed_order = dict(reversed(list(value.items())))
        assert canonical_json(reversed_order) == encoded
        assert content_sha256(reversed_order) == content_sha256(value)
