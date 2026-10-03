"""The DSL typecheck: JSON expression tree + column types -> IR, or every problem at once.

Errors are structured (a stable ``code``, a JSON-pointer ``path`` into the expression, and a message),
so a configuration screen can point at the exact node, and nothing is ever evaluated before it passes.
Limits keep hostile or runaway configuration cheap to reject: at most 32 levels deep and 500 nodes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from close_contracts.dsl import regex
from close_contracts.dsl.ast import MAX_STRING_LITERAL, Call, Col, DslType, Lit
from close_contracts.dsl.functions import DATE_FORMATS, FUNCTIONS, TIMESTAMP_FORMATS
from close_contracts.dsl.ir import Ir, IrCall, IrCol, IrLit, Value

MAX_DEPTH: Final = 32
MAX_NODES: Final = 500
BIGINT_MIN: Final = -(2**63)
BIGINT_MAX: Final = 2**63 - 1
_DECIMAL_INT_DIGITS: Final = 26  # DECIMAL(38,12)
_DECIMAL_SCALE: Final = 12
_CALENDAR: Final = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DECIMAL: Final = re.compile(r"^-?\d{1,26}(\.\d{1,12})?$")


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str


class DslTypeError(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.path or '/'}: {i.code}: {i.message}" for i in self.issues))


def typecheck(expr: Col | Lit | Call, schema: Mapping[str, DslType], *, expect: DslType | None = None) -> Ir:
    """Type the expression against ``schema`` (column name -> type); raise DslTypeError with every issue."""
    checker = _Checker(schema)
    ir = checker.check(expr, "", 1)
    if ir is not None and expect is not None and ir.type != expect:
        checker.issue("expected_type", "", f"the expression is {ir.type.value}, but {expect.value} is required")
    if checker.issues or ir is None:
        raise DslTypeError(checker.issues or [Issue("invalid", "", "the expression could not be typed")])
    return ir


class _Checker:
    def __init__(self, schema: Mapping[str, DslType]) -> None:
        self.schema = schema
        self.issues: list[Issue] = []
        self.nodes = 0

    def issue(self, code: str, path: str, message: str) -> None:
        self.issues.append(Issue(code, path, message))

    def check(self, node: Col | Lit | Call, path: str, depth: int) -> Ir | None:
        self.nodes += 1
        if self.nodes == MAX_NODES + 1:
            self.issue("too_many_nodes", path, f"expressions are limited to {MAX_NODES} nodes")
        if depth > MAX_DEPTH:
            self.issue("too_deep", path, f"expressions are limited to {MAX_DEPTH} levels")
            return None
        if isinstance(node, Col):
            type_ = self.schema.get(node.col)
            if type_ is None:
                self.issue("unknown_column", path, f"no column {node.col!r} here")
                return None
            return IrCol(node.col, type_)
        if isinstance(node, Lit):
            value = self.literal(node, path)
            return None if value is _BAD else IrLit(value, node.type)  # type: ignore[arg-type]
        return self.call(node, path, depth)

    def literal(self, node: Lit, path: str) -> Value | object:
        v, t = node.lit, node.type
        if v is None:
            return None
        ok = False
        out: Value = None
        if t == DslType.BOOLEAN:
            ok, out = isinstance(v, bool), v
        elif t == DslType.BIGINT:
            ok = isinstance(v, int) and not isinstance(v, bool) and BIGINT_MIN <= v <= BIGINT_MAX
            out = v
        elif t == DslType.STRING:
            ok, out = isinstance(v, str) and len(v) <= MAX_STRING_LITERAL, v
        elif t == DslType.DATE and isinstance(v, str) and _DATE.match(v):
            try:
                ok, out = True, date.fromisoformat(v)
            except ValueError:
                ok = False
        elif t == DslType.TIMESTAMP and isinstance(v, str) and _TIMESTAMP.match(v):
            try:
                ok, out = True, datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            except ValueError:
                ok = False
        elif t == DslType.DECIMAL and isinstance(v, str) and _DECIMAL.match(v):
            try:
                ok, out = True, Decimal(v)
            except InvalidOperation:
                ok = False
        if not ok:
            self.issue("bad_literal", path, f"{v!r} is not a valid {t.value} literal")
            return _BAD
        return out

    def call(self, node: Call, path: str, depth: int) -> Ir | None:
        overloads = FUNCTIONS.get(node.fn)
        if overloads is None:
            self.issue("unknown_function", path, f"no function {node.fn!r} in the catalog")
        args = [self.check(a, f"{path}/args/{i}", depth + 1) for i, a in enumerate(node.args)]
        if overloads is None or any(a is None for a in args):
            return None
        typed = [a for a in args if a is not None]
        types = tuple(a.type for a in typed)
        chosen = next((s for s in overloads if s.accepts(types)), None)
        if chosen is None:
            arities = {len(s.params) for s in overloads}
            variadic = any(s.variadic for s in overloads)
            if not variadic and len(types) not in arities:
                self.issue("arity", path, f"{node.fn} takes {sorted(arities)} arguments, got {len(types)}")
            else:
                got = ", ".join(t.value for t in types)
                self.issue("type_mismatch", path, f"{node.fn} does not accept ({got})")
            return None
        for position in sorted(chosen.literal):
            if not isinstance(typed[position], IrLit) or typed[position].value is None:  # type: ignore[union-attr]
                self.issue(
                    "not_literal", f"{path}/args/{position}", f"argument {position} of {node.fn} must be a literal"
                )
                return None
        if not self.literal_arguments(node.fn, typed, path):
            return None
        if node.fn in {"and", "or"}:
            flat: list[Ir] = []
            for a in typed:
                flat.extend(a.args if isinstance(a, IrCall) and a.fn == node.fn else (a,))
            typed = flat
        return IrCall(node.fn, tuple(typed), chosen.returns)

    def literal_arguments(self, fn: str, args: list[Ir], path: str) -> bool:
        """Validate the values of literal-only arguments (patterns, formats, group numbers, calendars)."""

        def value(i: int) -> object:
            arg = args[i]
            return arg.value if isinstance(arg, IrLit) else None

        at = f"{path}/args"
        if fn in {"regex_extract", "regex_matches"}:
            try:
                pattern = regex.validate(str(value(1)))
            except regex.RegexError as exc:
                self.issue(f"regex_{exc.code}", f"{at}/1", str(exc))
                return False
            if fn == "regex_extract" and not 0 <= int(value(2)) <= pattern.groups:  # type: ignore[call-overload]
                self.issue("bad_literal", f"{at}/2", f"the pattern has {pattern.groups} groups")
                return False
        elif fn == "split_part":
            if value(1) == "" or int(value(2)) < 1:  # type: ignore[call-overload]
                self.issue("bad_literal", at, "split_part needs a non-empty delimiter and a part number of 1 or more")
                return False
        elif fn == "parse_implied_decimals":
            if not 0 <= int(value(1)) <= _DECIMAL_SCALE:  # type: ignore[call-overload]
                self.issue("bad_literal", f"{at}/1", f"implied decimals must be 0 to {_DECIMAL_SCALE}")
                return False
        elif fn == "to_date" and value(1) not in DATE_FORMATS:
            self.issue("bad_literal", f"{at}/1", f"date formats are {sorted(DATE_FORMATS)}")
            return False
        elif fn == "to_timestamp" and value(1) not in TIMESTAMP_FORMATS:
            self.issue("bad_literal", f"{at}/1", f"timestamp formats are {sorted(TIMESTAMP_FORMATS)}")
            return False
        elif fn == "business_days_between" and not _CALENDAR.match(str(value(2))):
            self.issue("bad_literal", f"{at}/2", "a calendar code looks like US_FED or TARGET2")
            return False
        return True


_BAD: Final = object()
