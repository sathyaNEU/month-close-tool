"""Typechecking a match set's expressions against the canonical item fields.

The pydantic model checks a match set's shape. This checks what the model cannot: every expression types
against the prefixed item fields of the roles it reads (``a.amount_minor``), and each condition's operands
have the types the condition needs. Keys compare equal types, amounts are integers in minor units, date
windows compare dates, and text conditions compare strings. The app runs it when a version is saved, and
the engine compiles only match sets that pass it.
"""

from __future__ import annotations

from typing import Any

from close_contracts.binding import ITEM_FIELD_TYPES
from close_contracts.config.match_set import (
    AmountTolerance,
    DateWindow,
    ExpressionCondition,
    KeyEquals,
    MatchSet,
    RuleKind,
    TextMatch,
)
from close_contracts.dsl import DslType, DslTypeError, Ir, Issue, typecheck


def item_schema(*roles: str) -> dict[str, DslType]:
    """The canonical item fields prefixed with each role (``a.reference``)."""
    return {f"{role}.{name}": type_ for role in roles for name, type_ in ITEM_FIELD_TYPES.items()}


class _Checker:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def check(self, expr: Any, schema: dict[str, DslType], path: str, expect: DslType | None = None) -> Ir | None:
        try:
            return typecheck(expr, schema, expect=expect)
        except DslTypeError as exc:
            self.issues.extend(Issue(i.code, f"{path}{i.path}", i.message) for i in exc.issues)
            return None

    def operands(self, condition: Any, left: dict[str, DslType], right: dict[str, DslType], path: str) -> None:
        wanted = {AmountTolerance: DslType.BIGINT, DateWindow: DslType.DATE, TextMatch: DslType.STRING}
        expect = next((t for kind, t in wanted.items() if isinstance(condition, kind)), None)
        lhs = self.check(condition.left, left, f"{path}/left", expect)
        rhs = self.check(condition.right, right, f"{path}/right", expect)
        if isinstance(condition, KeyEquals) and lhs is not None and rhs is not None and lhs.type != rhs.type:
            self.issues.append(Issue("type_mismatch", path, f"the key compares {lhs.type.value} with {rhs.type.value}"))


def match_set_issues(ms: MatchSet) -> list[Issue]:
    """Every expression problem in the match set, with JSON-pointer-like paths (empty when it is sound)."""
    c = _Checker()
    for i, binding in enumerate(ms.sources):
        if binding.scope is not None:
            c.check(binding.scope, item_schema(binding.role.value), f"/sources/{i}/scope", DslType.BOOLEAN)
    for i, rule in enumerate(ms.pass_rules):
        left, right = rule.sides[0].value, rule.sides[-1].value
        left_schema, right_schema = item_schema(left), item_schema(right)
        for j, condition in enumerate(rule.conditions):
            path = f"/pass_rules/{i}/conditions/{j}"
            if isinstance(condition, ExpressionCondition):
                c.check(condition.expr, item_schema(left, right), f"{path}/expr", DslType.BOOLEAN)
            elif hasattr(condition, "left"):
                c.operands(condition, left_schema, right_schema, path)
        many = right_schema if rule.kind == RuleKind.ONE_TO_MANY else left_schema
        for k, expr in enumerate(rule.group_by):
            c.check(expr, many, f"/pass_rules/{i}/group_by/{k}")
    for i, timing in enumerate(ms.timing_rules):
        c.check(timing.when, item_schema(timing.role.value), f"/timing_rules/{i}/when", DslType.BOOLEAN)
    return c.issues
