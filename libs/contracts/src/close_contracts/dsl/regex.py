"""The regular-expression subset the rule DSL accepts, and its translation for Python.

A pattern runs in two engines: Java's ``java.util.regex`` inside Spark (``regexp_extract``, ``rlike``)
and Python's ``re`` in the reference interpreter. The subset is what both engines read the same way
and what cannot backtrack catastrophically:

* literals, ``.``, character classes ``[...]`` with ranges and negation;
* the escapes ``\\d \\D \\w \\W \\s \\S \\b \\B``, ``\\t \\n \\r \\f``, ``\\xhh``, ``\\uhhhh`` and any
  escaped punctuation;
* capturing groups ``(...)``, non-capturing groups ``(?:...)`` and alternation ``|``;
* quantifiers ``* + ? {n} {n,} {n,m}`` (n and m at most 1,000) and their lazy forms;
* the anchor ``^`` at the very start, and a leading ``(?i)``.

Everything else is rejected with a structured error: backreferences, lookaround, named and atomic
groups, possessive quantifiers, other inline flags, ``$`` (Java and Python treat a trailing line
terminator differently), POSIX and ``\\p`` classes, class intersection, unescaped ``]``, ``}`` or a
stray ``-`` in a class, and any quantifier on a group that itself contains a quantifier or an
alternation, which are the classic catastrophic shapes ``(a+)+`` and ``(a|a)*``. A pattern may have
at most four unbounded quantifiers and at most 256 characters.

Python is made to agree with Java by compiling with ``re.ASCII`` (Java's classes are ASCII by
default) and translating ``.`` outside a class to Java's default meaning, any character except a
line terminator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

MAX_LENGTH: Final = 256
MAX_REPEAT: Final = 1000
MAX_UNBOUNDED: Final = 4
_JAVA_DOT: Final = "[^\\n\\r\\u0085\\u2028\\u2029]"
_CLASS_ESCAPES: Final = frozenset("dDwWsS")
_BOUNDARY_ESCAPES: Final = frozenset("bB")
_CONTROL_ESCAPES: Final = frozenset("tnrf")
_HEX: Final = frozenset("0123456789abcdefABCDEF")


class RegexError(ValueError):
    """A pattern outside the accepted subset. ``code`` is stable and machine-readable."""

    def __init__(self, code: str, message: str, position: int) -> None:
        super().__init__(f"{code} at position {position}: {message}")
        self.code = code
        self.position = position


@dataclass(frozen=True)
class SafeRegex:
    """A validated pattern: ``pattern`` is what Spark receives, ``python`` its translation for ``re``."""

    pattern: str
    python: str
    groups: int
    ignore_case: bool

    def compile(self) -> re.Pattern[str]:
        return re.compile(self.python, re.ASCII | (re.IGNORECASE if self.ignore_case else 0))


@dataclass(frozen=True)
class _Shape:
    quantified: bool = False
    alternation: bool = False
    assertion: bool = False  # ^, \b, \B: zero-width, so it cannot be repeated

    def merge(self, other: _Shape) -> _Shape:
        return _Shape(self.quantified or other.quantified, self.alternation or other.alternation, False)


def validate(pattern: str) -> SafeRegex:
    """Return the validated pattern, or raise :class:`RegexError`."""
    if not isinstance(pattern, str) or not pattern:
        raise RegexError("empty", "a pattern must be a non-empty string", 0)
    if len(pattern) > MAX_LENGTH:
        raise RegexError("too_long", f"patterns are limited to {MAX_LENGTH} characters", MAX_LENGTH)
    parser = _Parser(pattern)
    return parser.run()


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.out: list[str] = []
        self.groups = 0
        self.unbounded = 0
        self.start = 0

    def _peek(self, ahead: int = 0) -> str | None:
        i = self.pos + ahead
        return self.text[i] if i < len(self.text) else None

    def _fail(self, code: str, message: str) -> RegexError:
        return RegexError(code, message, self.pos)

    def run(self) -> SafeRegex:
        ignore_case = False
        if self.text.startswith("(?i)"):
            ignore_case = True
            self.pos = self.start = 4
            if self.pos == len(self.text):
                raise self._fail("empty", "a pattern needs something after (?i)")
        self._alternation()
        if self.pos != len(self.text):
            raise self._fail("unbalanced", "unexpected ')'")
        return SafeRegex(self.text, "".join(self.out), self.groups, ignore_case)

    def _alternation(self) -> _Shape:
        shape = self._sequence()
        while self._peek() == "|":
            self.out.append("|")
            self.pos += 1
            shape = shape.merge(self._sequence())
            shape = _Shape(shape.quantified, True)
        return shape

    def _sequence(self) -> _Shape:
        shape = _Shape()
        while (ch := self._peek()) is not None and ch not in "|)":
            shape = shape.merge(self._term())
        return shape

    def _term(self) -> _Shape:
        shape = self._atom()
        if self._peek() in {"*", "+", "?", "{"}:
            if shape.assertion:
                raise self._fail("nothing_to_repeat", "an anchor or word boundary cannot be repeated")
            if shape.quantified or shape.alternation:
                raise self._fail(
                    "nested_quantifier",
                    "a repeated group may not contain a quantifier or an alternation (catastrophic backtracking)",
                )
            self._quantifier()
            return _Shape(quantified=True, alternation=shape.alternation)
        return shape

    def _quantifier(self) -> None:
        ch = self._peek()
        if ch == "{":
            m = re.compile(r"\{(\d{1,4})(,(\d{0,4}))?\}").match(self.text, self.pos)
            if m is None:
                raise self._fail("bad_repeat", "'{' must start a repeat like {2}, {2,} or {2,5}, or be escaped")
            low = int(m.group(1))
            high = m.group(3)
            if low > MAX_REPEAT or (high and int(high) > MAX_REPEAT):
                raise self._fail("repeat_too_large", f"repeat counts are limited to {MAX_REPEAT}")
            if high and int(high) < low:
                raise self._fail("bad_repeat", "the upper repeat count is below the lower one")
            if m.group(2) is not None and not high:
                self.unbounded += 1
            self.out.append(m.group(0))
            self.pos = m.end()
        else:
            if ch in {"*", "+"}:
                self.unbounded += 1
            self.out.append(str(ch))
            self.pos += 1
        if self.unbounded > MAX_UNBOUNDED:
            raise self._fail("too_many_unbounded", f"at most {MAX_UNBOUNDED} unbounded quantifiers are allowed")
        nxt = self._peek()
        if nxt == "?":
            self.out.append("?")
            self.pos += 1
        elif nxt == "+":
            raise self._fail("possessive", "possessive quantifiers are Java-only")
        if self._peek() in {"*", "+", "?", "{"}:
            raise self._fail("nothing_to_repeat", "a quantifier cannot follow another quantifier")

    def _atom(self) -> _Shape:
        ch = self._peek()
        assert ch is not None  # noqa: S101 (the caller checked)
        if ch == "(":
            return self._group()
        if ch == "[":
            self._class()
            return _Shape()
        if ch == ".":
            self.out.append(_JAVA_DOT)
            self.pos += 1
            return _Shape()
        if ch == "^":
            if self.pos != self.start:
                raise self._fail("anchor_position", "'^' is only allowed at the very start of the pattern")
            self.out.append("^")
            self.pos += 1
            return _Shape(assertion=True)
        if ch == "$":
            raise self._fail("end_anchor", "'$' behaves differently in Java and Python; use ends_with instead")
        if ch in "*+?{":
            raise self._fail("nothing_to_repeat", f"'{ch}' has nothing to repeat")
        if ch in "]}":
            raise self._fail("unescaped", f"escape a literal '{ch}' as '\\{ch}'")
        if ch == "\\":
            return self._escape(in_class=False)
        self.out.append(ch)
        self.pos += 1
        return _Shape()

    def _group(self) -> _Shape:
        self.pos += 1  # "("
        if self._peek() == "?":
            if self._peek(1) != ":":
                raise self._fail(
                    "unsupported_group", "only (...) and (?:...) groups are allowed (no lookaround, names or flags)"
                )
            self.out.append("(?:")
            self.pos += 2
        else:
            self.groups += 1
            self.out.append("(")
        shape = self._alternation()
        if self._peek() != ")":
            raise self._fail("unbalanced", "missing ')'")
        self.out.append(")")
        self.pos += 1
        return _Shape(shape.quantified, shape.alternation)

    def _class(self) -> None:
        self.out.append("[")
        self.pos += 1
        if self._peek() == "^":
            self.out.append("^")
            self.pos += 1
        if self._peek() == "]":
            raise self._fail("empty_class", "a class cannot start with ']'; escape it as '\\]'")
        items = 0
        while True:
            ch = self._peek()
            if ch is None:
                raise self._fail("unbalanced", "missing ']'")
            if ch == "]":
                break
            if ch == "[":
                raise self._fail("nested_class", "classes cannot be nested or combined (Java unions and POSIX classes)")
            if ch == "&" and self._peek(1) == "&":
                raise self._fail("class_intersection", "class intersection '&&' is Java-only")
            low = self._class_char()
            if self._peek() == "-" and self._peek(1) not in {"]", None}:
                self.out.append("-")
                self.pos += 1
                high = self._class_char()
                if low is None or high is None:
                    raise self._fail("bad_range", "a range needs a single character at both ends")
                if ord(high) < ord(low):
                    raise self._fail("bad_range", "the range ends before it starts")
            items += 1
        self.out.append("]")
        self.pos += 1

    def _class_char(self) -> str | None:
        """One class member; returns the literal character, or None for an escape like \\d."""
        ch = self._peek()
        if ch == "-":
            raise self._fail("unescaped", "escape a literal '-' in a class as '\\-'")
        if ch == "\\":
            before = len(self.out)
            self._escape(in_class=True)
            written = "".join(self.out[before:])
            return _literal_of(written)
        assert ch is not None  # noqa: S101 (the loop checked)
        self.out.append(ch)
        self.pos += 1
        return ch

    def _escape(self, *, in_class: bool) -> _Shape:
        nxt = self._peek(1)
        if nxt is None:
            raise self._fail("trailing_backslash", "the pattern ends with a backslash")
        if nxt in _CLASS_ESCAPES or nxt in _CONTROL_ESCAPES:
            self.out.append("\\" + nxt)
            self.pos += 2
            return _Shape()
        if nxt in _BOUNDARY_ESCAPES:
            if in_class:
                raise self._fail("unsupported_escape", f"'\\{nxt}' is not allowed inside a class")
            self.out.append("\\" + nxt)
            self.pos += 2
            return _Shape(assertion=True)
        if nxt == "x":
            digits = self.text[self.pos + 2 : self.pos + 4]
            if len(digits) != 2 or not set(digits) <= _HEX:
                raise self._fail("bad_escape", "'\\x' needs exactly two hex digits")
            self.out.append("\\x" + digits)
            self.pos += 4
            return _Shape()
        if nxt == "u":
            digits = self.text[self.pos + 2 : self.pos + 6]
            if len(digits) != 4 or not set(digits) <= _HEX:
                raise self._fail("bad_escape", "'\\u' needs exactly four hex digits")
            self.out.append("\\u" + digits)
            self.pos += 6
            return _Shape()
        if nxt.isascii() and nxt.isalnum():
            raise self._fail("unsupported_escape", f"'\\{nxt}' is not in the portable subset")
        self.out.append("\\" + nxt)
        self.pos += 2
        return _Shape()


def _literal_of(escape: str) -> str | None:
    """The single character an escape stands for, or None for a class like \\d."""
    if len(escape) == 2 and escape[1] in _CLASS_ESCAPES:
        return None
    if escape in {"\\t", "\\n", "\\r", "\\f"}:
        return {"\\t": "\t", "\\n": "\n", "\\r": "\r", "\\f": "\f"}[escape]
    if escape.startswith(("\\x", "\\u")):
        return chr(int(escape[2:], 16))
    return escape[1]
