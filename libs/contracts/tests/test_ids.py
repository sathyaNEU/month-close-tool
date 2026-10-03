from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import resources

import pytest
from hypothesis import given
from hypothesis import strategies as st

from close_contracts.ids import (
    ID_HEX_LEN,
    IdPartTypeError,
    canonical_part,
    encode_part,
    hash_id,
    id_payload,
    validate_namespace,
)


def _load_vectors() -> list[dict[str, object]]:
    text = resources.files("close_contracts").joinpath("test_vectors/ids.json").read_text(encoding="utf-8")
    return list(json.loads(text))


def _decode_part(p: dict[str, object]) -> str | int | bool | date | None:
    t = p["t"]
    if t == "null":
        return None
    if t == "date":
        return date.fromisoformat(str(p["v"]))
    v = p["v"]
    assert isinstance(v, str | int | bool)
    return v


def test_payload_matches_the_written_spec() -> None:
    # H(ns, p...) hashes ns + US + US.join(enc(p)), enc(None) = "-1:", enc(s) = f"{len(s)}:{s}"
    assert id_payload("itm", "a", None, "bc") == "itm\x1f1:a\x1f-1:\x1f2:bc"
    expected = hashlib.sha256(b"itm\x1f1:a\x1f-1:\x1f2:bc").hexdigest()[:32]
    assert hash_id("itm", "a", None, "bc") == expected


def test_id_is_128_bits_of_lowercase_hex() -> None:
    value = hash_id("grp", "run-1", 7)
    assert len(value) == ID_HEX_LEN == 32
    assert value == value.lower()
    int(value, 16)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (("a", None, "b"), ("a", "b", None)),  # concat_ws would collapse these
        ((None,), ("",)),
        (("",), ()),
        (("a\x1fb",), ("a", "b")),
        (("1:a",), ("a",)),
        ((1,), ("1",)),  # same string form, but distinct tuples still hash equal by design
    ],
)
def test_ambiguous_looking_inputs(left: tuple[object, ...], right: tuple[object, ...]) -> None:
    same_canonical = [canonical_part(p) for p in left] == [canonical_part(p) for p in right]  # type: ignore[arg-type]
    assert (hash_id("itm", *left) == hash_id("itm", *right)) is same_canonical  # type: ignore[arg-type]


def test_canonical_forms_match_spark_casts() -> None:
    assert canonical_part(True) == "true"
    assert canonical_part(False) == "false"
    assert canonical_part(-42) == "-42"
    assert canonical_part(date(2026, 9, 26)) == "2026-09-26"
    assert canonical_part("x") == "x"
    assert canonical_part(None) is None
    assert encode_part(None) == "-1:"
    assert encode_part("\U0001f600") == "1:\U0001f600"  # code points, like Spark length()


@pytest.mark.parametrize("bad", [datetime(2026, 9, 26, 12, 0, tzinfo=UTC), 1.5, Decimal("1.0"), b"x", object()])
def test_unsupported_part_types_are_rejected(bad: object) -> None:
    with pytest.raises(IdPartTypeError):
        hash_id("itm", bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("ns", ["", "a", "Itm", "1ab", "itm-x", "a" * 17, "itm "])
def test_invalid_namespaces_are_rejected(ns: str) -> None:
    with pytest.raises(ValueError, match="invalid ID namespace"):
        validate_namespace(ns)


def test_vectors_file_matches_implementation() -> None:
    vectors = _load_vectors()
    assert len(vectors) >= 50
    for v in vectors:
        parts = [_decode_part(p) for p in v["parts"]]  # type: ignore[attr-defined]
        ns = str(v["ns"])
        assert id_payload(ns, *parts).encode("utf-8").hex() == v["payload_utf8_hex"]
        assert hash_id(ns, *parts) == v["expected"]


_part = st.one_of(
    st.none(),
    st.text(max_size=12),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.booleans(),
    st.dates(),
)


@given(st.lists(_part, max_size=5), st.lists(_part, max_size=5))
def test_payload_encoding_is_injective(a: list[object], b: list[object]) -> None:
    ca = [canonical_part(p) for p in a]  # type: ignore[arg-type]
    cb = [canonical_part(p) for p in b]  # type: ignore[arg-type]
    if ca != cb:
        assert id_payload("itm", *a) != id_payload("itm", *b)  # type: ignore[arg-type]
    else:
        assert id_payload("itm", *a) == id_payload("itm", *b)  # type: ignore[arg-type]
