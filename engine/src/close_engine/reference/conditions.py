"""Pass-rule conditions for the reference matcher, typechecked once against the two sides' item fields.

Operands are DSL expressions over prefixed item fields (``a.amount_minor``, ``b.reference``). A rule's
first side is ``left`` and its last side is ``right``, for every kind.

- **Pairwise conditions** (text, dates, equal currency and free expressions) are lowered to one boolean DSL
  expression over both sides, so the interpreter here and the Spark compiler later share one definition.
  Only fuzzy text stays outside the DSL (Levenshtein or Jaro-Winkler similarity in basis points).
- **Keys** are compared as tuples, and a null or empty key value never matches.
- **Amount and sign conditions** are aggregate: each side's values are summed over the side's items (one item
  on the one side of ``1:1``, ``1:M`` and ``M:1``). A basis-point tolerance is taken of the one side's
  total, which is the left side except in ``M:1``, so the subset search has a fixed tolerance. A ``sign``
  condition in mode ``absolute`` compares absolute totals; ``same`` and ``opposite`` check the signs of the
  two sides' ``amount_minor`` totals and leave the amount comparison as written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from close_contracts.binding import ITEM_FIELD_TYPES
from close_contracts.calendar import BusinessCalendar
from close_contracts.config.match_set import (
    AmountTolerance,
    CurrencyRule,
    DateWindow,
    ExpressionCondition,
    KeyEquals,
    PassRule,
    RuleKind,
    SignRule,
    TextMatch,
)
from close_contracts.config.validate import item_schema
from close_contracts.dsl import DslType, Ir, dump, evaluate, parse, typecheck

Calendars = Mapping[str, BusinessCalendar]
Row = Mapping[str, Any]


schema_for = item_schema  # the prefixed item fields, shared with the app's validation


def view(role: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    return {f"{role}.{name}": fields.get(name) for name in ITEM_FIELD_TYPES}


def _fn(name: str, *args: Any) -> dict[str, Any]:
    return {"fn": name, "args": list(args)}


def _int(value: int) -> dict[str, Any]:
    return {"lit": value, "type": "bigint"}


def _str(value: str) -> dict[str, Any]:
    return {"lit": value, "type": "string"}


def _nonempty(expr: Any) -> dict[str, Any]:
    return _fn("ne", expr, _str(""))


def _lower_text(c: TextMatch) -> dict[str, Any]:
    left, right = dump(c.left), dump(c.right)
    if c.op == "equals":
        return _fn("eq", left, right)
    if c.op == "normalized_equals":
        norm = _fn("normalize_text", left)
        return _fn("and", _nonempty(norm), _fn("eq", norm, _fn("normalize_text", right)))
    if c.op in {"contains", "starts_with", "ends_with"}:  # the right operand is the needle
        return _fn("and", _fn("ge", _fn("length", right), _int(c.min_needle_length)), _fn(c.op, left, right))
    extract = [_fn("regex_extract", side, _str(str(c.pattern)), _int(c.group)) for side in (left, right)]
    return _fn("and", _nonempty(extract[0]), _fn("eq", extract[0], extract[1]))


def _lower_date(c: DateWindow) -> dict[str, Any]:
    left, right = dump(c.left), dump(c.right)
    if c.unit == "calendar_days":
        offset = _fn("days_between", left, right)
    else:
        offset = _fn("business_days_between", left, right, _str(str(c.calendar)))
    return _fn("and", _fn("ge", offset, _int(-c.before)), _fn("le", offset, _int(c.after)))


@dataclass(frozen=True)
class Compiled:
    kind: RuleKind
    roles: tuple[str, str]  # (left, right); the same role twice for self_net
    keys: tuple[tuple[Ir, Ir], ...]
    pair: Ir | None  # every pairwise condition except fuzzy text, as one boolean expression
    fuzzy: tuple[tuple[TextMatch, Ir, Ir], ...]
    amounts: tuple[tuple[AmountTolerance, Ir, Ir], ...]
    signs: tuple[str, ...]
    group_by: tuple[Ir, ...]  # over the many side (1:M, M:1) or the only side (self_net)

    @property
    def base(self) -> str:
        """The side whose total a basis-point tolerance is taken of."""
        return "right" if self.kind == RuleKind.MANY_TO_ONE else "left"


def compile_rule(rule: PassRule) -> Compiled:
    left_role, right_role = rule.sides[0].value, rule.sides[-1].value
    left_schema, right_schema = schema_for(left_role), schema_for(right_role)
    both = schema_for(left_role, right_role)
    keys, pairwise, fuzzy, amounts, signs = [], [], [], [], []
    for c in rule.conditions:
        if isinstance(c, KeyEquals):
            keys.append((typecheck(c.left, left_schema), typecheck(c.right, right_schema)))
        elif isinstance(c, AmountTolerance):
            amounts.append((c, typecheck(c.left, left_schema), typecheck(c.right, right_schema)))
        elif isinstance(c, SignRule):
            signs.append(c.mode)
        elif isinstance(c, CurrencyRule):
            if c.mode != "equal":
                raise NotImplementedError("FX-converted currency matching is not in the reference matcher yet")
            pairwise.append(_fn("eq", {"col": f"{left_role}.currency"}, {"col": f"{right_role}.currency"}))
        elif isinstance(c, ExpressionCondition):
            pairwise.append(dump(c.expr))
        elif isinstance(c, TextMatch):
            typecheck(c.left, left_schema)  # each operand reads only its own side
            typecheck(c.right, right_schema)
            if c.op == "fuzzy":
                fuzzy.append((c, typecheck(c.left, left_schema), typecheck(c.right, right_schema)))
            else:
                pairwise.append(_lower_text(c))
        elif isinstance(c, DateWindow):
            typecheck(c.left, left_schema)
            typecheck(c.right, right_schema)
            pairwise.append(_lower_date(c))
    pair = None
    if pairwise:
        expr = pairwise[0] if len(pairwise) == 1 else _fn("and", *pairwise)
        pair = typecheck(parse(expr), both, expect=DslType.BOOLEAN)
    many = {RuleKind.ONE_TO_MANY: right_schema, RuleKind.MANY_TO_ONE: left_schema, RuleKind.SELF_NET: left_schema}
    group_by = tuple(typecheck(g, many[rule.kind]) for g in rule.group_by) if rule.kind in many else ()
    return Compiled(
        rule.kind,
        (left_role, right_role),
        tuple(keys),
        pair,
        tuple(fuzzy),
        tuple(amounts),
        tuple(signs),
        group_by,
    )


def _key(irs: Sequence[Ir], row: Row, calendars: Calendars) -> tuple[Any, ...] | None:
    values = tuple(evaluate(ir, row, calendars=calendars) for ir in irs)
    return None if any(v is None or v == "" for v in values) else values


def left_key(c: Compiled, row: Row, calendars: Calendars) -> tuple[Any, ...] | None:
    return _key([k[0] for k in c.keys], row, calendars)


def right_key(c: Compiled, row: Row, calendars: Calendars) -> tuple[Any, ...] | None:
    return _key([k[1] for k in c.keys], row, calendars)


def group_key(c: Compiled, row: Row, calendars: Calendars) -> tuple[Any, ...] | None:
    return _key(c.group_by, row, calendars)


def _levenshtein(x: str, y: str) -> int:
    previous = list(range(len(y) + 1))
    for i, cx in enumerate(x, 1):
        current = [i]
        for j, cy in enumerate(y, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (cx != cy)))
        previous = current
    return previous[-1]


def _jaro_winkler(x: str, y: str) -> float:
    if x == y:
        return 1.0
    window = max(max(len(x), len(y)) // 2 - 1, 0)
    xm, ym = [False] * len(x), [False] * len(y)
    matches = 0
    for i, cx in enumerate(x):
        for j in range(max(0, i - window), min(len(y), i + window + 1)):
            if not ym[j] and y[j] == cx:
                xm[i] = ym[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    xs = [c for c, m in zip(x, xm, strict=True) if m]
    ys = [c for c, m in zip(y, ym, strict=True) if m]
    transpositions = sum(a != b for a, b in zip(xs, ys, strict=True)) // 2
    # Jaro-Winkler is defined in floating point; the result is quantized to basis points below.
    parts = matches / len(x) + matches / len(y)  # spark-ban: allow SB008 Python float by definition
    jaro = (parts + (matches - transpositions) / matches) / 3  # spark-ban: allow SB008 Python float by definition
    prefix = 0
    for a, b in zip(x[:4], y[:4], strict=False):
        if a != b:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def similarity_bps(algorithm: str, x: str, y: str) -> int:
    """Similarity quantized to integer basis points (10,000 means identical)."""
    if algorithm == "levenshtein":
        longest = max(len(x), len(y)) or 1
        return (10_000 * (longest - _levenshtein(x, y))) // longest
    return int(_jaro_winkler(x, y) * 10_000)


def pair_ok(c: Compiled, row: Row, calendars: Calendars) -> bool:
    """The pairwise conditions on a combined row holding both items' prefixed fields."""
    if c.pair is not None and evaluate(c.pair, row, calendars=calendars) is not True:
        return False
    for condition, left_ir, right_ir in c.fuzzy:
        left = evaluate(left_ir, row, calendars=calendars)
        right = evaluate(right_ir, row, calendars=calendars)
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        if similarity_bps(str(condition.algorithm), left, right) < int(condition.min_similarity_bps or 0):
            return False
    return True


Totals = tuple[int, ...]


def values(c: Compiled, side: str, row: Row, calendars: Calendars) -> Totals | None:
    """One item's contribution to its side's totals: each amount operand, then ``amount_minor``.

    None when an operand is null, which keeps the item out of any group this rule forms."""
    right = side == "right"
    out = [evaluate(r if right else lf, row, calendars=calendars) for _, lf, r in c.amounts]
    out.append(row[f"{c.roles[1 if right else 0]}.amount_minor"])
    ints = [v for v in out if isinstance(v, int) and not isinstance(v, bool)]
    return tuple(ints) if len(ints) == len(out) else None


def add(x: Totals, y: Totals) -> Totals:
    return tuple(a + b for a, b in zip(x, y, strict=True))


def _sign(x: int) -> int:
    return (x > 0) - (x < 0)


def allowed(condition: AmountTolerance, base: int) -> int:
    part = abs(base) * condition.bps // 10_000
    return {
        "exact": 0,
        "absolute": condition.absolute_minor,
        "bps": part,
        "bps_capped": min(part, condition.cap_minor),
    }[condition.mode]


def totals_ok(c: Compiled, left: Totals, right: Totals) -> bool:
    """The amount and sign conditions on the two sides' summed values."""
    absolute = "absolute" in c.signs
    for i, (condition, _, _) in enumerate(c.amounts):
        lv, rv = (abs(left[i]), abs(right[i])) if absolute else (left[i], right[i])
        if abs(lv - rv) > allowed(condition, lv if c.base == "left" else rv):
            return False
    if "same" in c.signs and _sign(left[-1]) != _sign(right[-1]):
        return False
    return not ("opposite" in c.signs and _sign(left[-1]) != -_sign(right[-1]))


def score(c: Compiled, left: Totals, right: Totals) -> int:
    """How far apart the two sides are: the summed absolute amount differences (0 is a perfect match)."""
    absolute = "absolute" in c.signs
    total = 0
    for i in range(len(c.amounts)):
        lv, rv = (abs(left[i]), abs(right[i])) if absolute else (left[i], right[i])
        total += abs(lv - rv)
    return total
