"""The DSL's function catalog: every function a rule or mapping may call, with its typed overloads.

Nothing outside this catalog can be called, so the Spark compiler (Stage 3) and the Python interpreter
implement exactly this list, and a differential test holds them to each other. Parameters listed in
``literal`` must be literals (regex patterns, formats, calendar names, group numbers), which lets the
typecheck validate them before anything runs.

Null handling follows SQL: a function returns null when any argument is null, except ``and``/``or``
(three-valued logic), ``not``, ``is_null``/``is_not_null``, ``coalesce`` and ``if``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from close_contracts.dsl.ast import DslType

STR, INT, BOOL, DAY, TS, DEC = (
    DslType.STRING,
    DslType.BIGINT,
    DslType.BOOLEAN,
    DslType.DATE,
    DslType.TIMESTAMP,
    DslType.DECIMAL,
)
ORDERED: Final = (STR, INT, DAY, TS, DEC)
ANY: Final = (STR, INT, BOOL, DAY, TS, DEC)

DATE_FORMATS: Final = frozenset({"yyyy-MM-dd", "yyyyMMdd", "yyMMdd", "MM/dd/yyyy", "dd/MM/yyyy", "dd.MM.yyyy"})
TIMESTAMP_FORMATS: Final = frozenset({"yyyy-MM-dd'T'HH:mm:ss'Z'", "yyyy-MM-dd HH:mm:ss"})


@dataclass(frozen=True)
class Signature:
    params: tuple[DslType, ...]
    returns: DslType
    variadic: bool = False  # the last parameter repeats; at least len(params) arguments
    literal: frozenset[int] = field(default_factory=frozenset)  # argument positions that must be literals

    def accepts(self, args: tuple[DslType, ...]) -> bool:
        if self.variadic:
            if len(args) < len(self.params):
                return False
            expected = self.params + (self.params[-1],) * (len(args) - len(self.params))
        else:
            if len(args) != len(self.params):
                return False
            expected = self.params
        return all(a == e for a, e in zip(args, expected, strict=True))


def _each(types: Iterable[DslType], make: object) -> tuple[Signature, ...]:
    return tuple(make(t) for t in types)  # type: ignore[operator]


FUNCTIONS: Final[dict[str, tuple[Signature, ...]]] = {
    # logic
    "and": (Signature((BOOL, BOOL), BOOL, variadic=True),),
    "or": (Signature((BOOL, BOOL), BOOL, variadic=True),),
    "not": (Signature((BOOL,), BOOL),),
    "if": _each(ANY, lambda t: Signature((BOOL, t, t), t)),
    "is_null": _each(ANY, lambda t: Signature((t,), BOOL)),
    "is_not_null": _each(ANY, lambda t: Signature((t,), BOOL)),
    "coalesce": _each(ANY, lambda t: Signature((t, t), t, variadic=True)),
    # comparison
    "eq": _each(ANY, lambda t: Signature((t, t), BOOL)),
    "ne": _each(ANY, lambda t: Signature((t, t), BOOL)),
    "lt": _each(ORDERED, lambda t: Signature((t, t), BOOL)),
    "le": _each(ORDERED, lambda t: Signature((t, t), BOOL)),
    "gt": _each(ORDERED, lambda t: Signature((t, t), BOOL)),
    "ge": _each(ORDERED, lambda t: Signature((t, t), BOOL)),
    # text
    "lower": (Signature((STR,), STR),),
    "upper": (Signature((STR,), STR),),
    "trim": (Signature((STR,), STR),),
    "normalize_text": (Signature((STR,), STR),),  # lower case, ASCII letters and digits only
    "length": (Signature((STR,), INT),),
    "substring": (Signature((STR, INT, INT), STR),),  # 1-based start, length
    "concat": (Signature((STR, STR), STR, variadic=True),),
    "starts_with": (Signature((STR, STR), BOOL),),
    "ends_with": (Signature((STR, STR), BOOL),),
    "contains": (Signature((STR, STR), BOOL),),
    "regex_extract": (Signature((STR, STR, INT), STR, literal=frozenset({1, 2})),),
    "regex_matches": (Signature((STR, STR), BOOL, literal=frozenset({1})),),
    "split_part": (Signature((STR, STR, INT), STR, literal=frozenset({1, 2})),),
    "cast_string": _each((INT, BOOL, DAY, DEC), lambda t: Signature((t,), STR)),
    # integers (amounts are BIGINT minor units, roadmap D9)
    "add": (Signature((INT, INT), INT),),
    "sub": (Signature((INT, INT), INT),),
    "mul": (Signature((INT, INT), INT),),
    "neg": (Signature((INT,), INT),),
    "abs": (Signature((INT,), INT),),
    "abs_diff": (Signature((INT, INT), INT),),
    "bps_of": (Signature((INT, INT), INT),),  # floor(|amount| * bps / 10,000)
    "parse_bigint": (Signature((STR,), INT),),
    # amounts in text: plain "1234.56", decimal comma "1234,56" (MT940), implied decimals "123456" (BAI2)
    "parse_decimal": (Signature((STR,), DEC),),
    "parse_decimal_comma": (Signature((STR,), DEC),),
    "parse_implied_decimals": (Signature((STR, INT), DEC, literal=frozenset({1})),),
    "to_minor_units": (Signature((DEC, STR), INT),),  # exact, or an evaluation error
    "currency_exponent": (Signature((STR,), INT),),
    # dates and times (UTC)
    "to_date": (Signature((STR, STR), DAY, literal=frozenset({1})),),
    "to_timestamp": (Signature((STR, STR), TS, literal=frozenset({1})),),
    "date_of": (Signature((TS,), DAY),),
    "date_add_days": (Signature((DAY, INT), DAY),),
    "days_between": (Signature((DAY, DAY), INT),),  # second minus first, calendar days
    "business_days_between": (Signature((DAY, DAY, STR), INT, literal=frozenset({2})),),
}
