"""The portable regex subset: what is accepted, what is rejected (with which code), and the translation."""

from __future__ import annotations

import re

import pytest
from regexcorpus import ACCEPTED, REJECTED

from close_contracts.dsl.regex import RegexError, validate


@pytest.mark.parametrize("pattern", ACCEPTED)
def test_portable_patterns_are_accepted(pattern: str) -> None:
    safe = validate(pattern)
    safe.compile()  # the translation is valid Python


@pytest.mark.parametrize(("pattern", "code"), REJECTED)
def test_unsafe_or_non_portable_patterns_are_rejected_with_a_code(pattern: str, code: str) -> None:
    with pytest.raises(RegexError) as info:
        validate(pattern)
    assert info.value.code == code, str(info.value)


def test_the_corpus_is_big_enough_for_the_gate() -> None:
    catastrophic = [p for p, code in REJECTED if code in {"nested_quantifier", "too_many_unbounded"}]
    assert len(catastrophic) >= 9
    assert len(REJECTED) >= 40


def test_dot_follows_java_and_stops_at_every_line_terminator() -> None:
    compiled = validate(r"a.b").compile()
    assert compiled.search("axb")
    for terminator in ("\n", "\r", "\u0085", "\u2028", "\u2029"):
        assert compiled.search(f"a{terminator}b") is None, repr(terminator)


def test_classes_are_ascii_like_java() -> None:
    assert validate(r"^\d+").compile().search("١٢") is None  # Arabic-Indic digits are not \d in Java
    assert validate(r"^\w+").compile().search("é") is None


def test_a_leading_ignore_case_flag_becomes_a_python_flag() -> None:
    safe = validate(r"(?i)^ref(\d+)")
    assert safe.ignore_case
    assert safe.groups == 1
    assert not safe.python.startswith("(?i)")
    assert safe.compile().search("REF42").group(1) == "42"  # type: ignore[union-attr]


def test_group_counts_ignore_non_capturing_groups() -> None:
    assert validate(r"(?:a)(b)(?:c(d))").groups == 2


def test_rejection_reports_the_position() -> None:
    with pytest.raises(RegexError) as info:
        validate(r"abc(a+)+")
    assert info.value.position == 7
    assert re.search(r"position 7", str(info.value))
