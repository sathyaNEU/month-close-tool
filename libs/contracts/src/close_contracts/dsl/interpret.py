"""The DSL's pure-Python interpreter, the reference semantics for the IR.

Stage 3 compiles the same IR to Spark Columns, and a differential test holds the two to each other, so
wherever Spark and Python could differ this module follows Spark (ANSI mode): ``trim`` removes spaces
only, ``substring`` uses Spark's 1-based rules, ``regex_extract`` returns an empty string when nothing
matches, a two-digit year ``yy`` means 2000 to 2099, integer overflow is an error, and
``cast_string`` of a decimal keeps all 12 decimal places. Anything the input cannot support (a bad
number, an impossible date, lost precision) raises :class:`DslEvalError`, which a mapping treats as a
rejected row.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache
from string import ascii_lowercase, digits
from typing import Any, Final

from close_contracts.calendar import BusinessCalendar
from close_contracts.dsl import regex
from close_contracts.dsl.ir import Ir, IrCall, IrCol, IrLit, Value
from close_contracts.money import MoneyError
from close_contracts.money import exponent as currency_exponent

BIGINT_MIN: Final = -(2**63)
BIGINT_MAX: Final = 2**63 - 1
_KEEP: Final = frozenset(ascii_lowercase + digits)
_SCALE: Final = Decimal("1e-12")


class DslEvalError(ValueError):
    """The input cannot be evaluated (the row is rejected, it is not a bug in the rule)."""


def evaluate(ir: Ir, row: Mapping[str, Any], *, calendars: Mapping[str, BusinessCalendar] | None = None) -> Value:
    """Evaluate ``ir`` against one row (column name -> Python value)."""
    return _Eval(row, calendars or {}).run(ir)


class _Eval:
    def __init__(self, row: Mapping[str, Any], calendars: Mapping[str, BusinessCalendar]) -> None:
        self.row = row
        self.calendars = calendars

    def run(self, ir: Ir) -> Value:
        if isinstance(ir, IrCol):
            value: Value = self.row[ir.name]
            return value
        if isinstance(ir, IrLit):
            return ir.value
        if ir.fn in _LAZY:
            return _LAZY[ir.fn](self, ir)
        args = [self.run(a) for a in ir.args]
        if any(a is None for a in args):
            return None  # strict functions: any null argument gives null
        if ir.fn == "business_days_between":
            code = str(args[2])
            if code not in self.calendars:
                raise DslEvalError(f"calendar {code} is not loaded")
            return self.calendars[code].business_days_between(args[0], args[1])  # type: ignore[arg-type]
        return _STRICT[ir.fn](*args)


def _and(ev: _Eval, ir: IrCall) -> Value:
    unknown = False
    for arg in ir.args:
        value = ev.run(arg)
        if value is False:
            return False
        unknown = unknown or value is None
    return None if unknown else True


def _or(ev: _Eval, ir: IrCall) -> Value:
    unknown = False
    for arg in ir.args:
        value = ev.run(arg)
        if value is True:
            return True
        unknown = unknown or value is None
    return None if unknown else False


def _if(ev: _Eval, ir: IrCall) -> Value:
    return ev.run(ir.args[1]) if ev.run(ir.args[0]) is True else ev.run(ir.args[2])


def _coalesce(ev: _Eval, ir: IrCall) -> Value:
    for arg in ir.args:
        value = ev.run(arg)
        if value is not None:
            return value
    return None


def _not(ev: _Eval, ir: IrCall) -> Value:
    value = ev.run(ir.args[0])
    return None if value is None else not value


def _is_null(ev: _Eval, ir: IrCall) -> Value:
    return ev.run(ir.args[0]) is None


def _is_not_null(ev: _Eval, ir: IrCall) -> Value:
    return ev.run(ir.args[0]) is not None


_LAZY: Final[dict[str, Callable[[_Eval, IrCall], Value]]] = {
    "and": _and,
    "or": _or,
    "if": _if,
    "coalesce": _coalesce,
    "not": _not,
    "is_null": _is_null,
    "is_not_null": _is_not_null,
}


def _bigint(value: int) -> int:
    if not BIGINT_MIN <= value <= BIGINT_MAX:
        raise DslEvalError("integer overflow")
    return value


def _substring(text: str, pos: int, length: int) -> str:
    n = len(text)  # Spark's UTF8String.substringSQL
    start = pos - 1 if pos > 0 else (n + pos if pos < 0 else 0)
    end = start + length
    start = max(start, 0)
    return "" if start >= end else text[start:end]


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    return regex.validate(pattern).compile()


def _regex_extract(text: str, pattern: str, group: int) -> str:
    m = _compiled(pattern).search(text)
    return "" if m is None else (m.group(group) or "")


def _split_part(text: str, delimiter: str, part: int) -> str:
    parts = text.split(delimiter)
    return parts[part - 1] if part <= len(parts) else ""


def _cast_string(value: Value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value.quantize(_SCALE), "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _parse(pattern: str, text: str, what: str) -> re.Match[str]:
    m = re.fullmatch(pattern, text)
    if m is None:
        raise DslEvalError(f"{text!r} is not {what}")
    return m


def _decimal(text: str) -> Decimal:
    value = Decimal(text)
    if abs(value) >= Decimal(10) ** 26:
        raise DslEvalError("the amount does not fit DECIMAL(38,12)")
    return value


def _parse_decimal(text: str) -> Decimal:
    return _decimal(_parse(r"[+-]?\d{1,26}(\.\d{1,12})?", text, "a decimal number").group(0))


def _parse_decimal_comma(text: str) -> Decimal:
    m = _parse(r"([+-]?\d{1,26})(,(\d{0,12}))?", text, "a decimal-comma amount")
    return _decimal(m.group(1) + "." + (m.group(3) or "0"))


def _parse_implied(text: str, decimals: int) -> Decimal:
    raw = _parse(r"[+-]?\d{1,38}", text, "an implied-decimal amount").group(0)
    return _decimal(str(Decimal(raw).scaleb(-decimals)))


def _to_minor(amount: Decimal, currency: str) -> int:
    try:
        scaled = amount.scaleb(currency_exponent(currency))
    except MoneyError as exc:
        raise DslEvalError(str(exc)) from exc
    if scaled != scaled.to_integral_value():
        raise DslEvalError(f"{amount} has more decimals than {currency} allows")
    return _bigint(int(scaled))


def _exponent(currency: str) -> int:
    try:
        return currency_exponent(currency)
    except MoneyError as exc:
        raise DslEvalError(str(exc)) from exc


_DATE_PATTERNS: Final = {
    "yyyy-MM-dd": (r"(\d{4})-(\d{2})-(\d{2})", "ymd"),
    "yyyyMMdd": (r"(\d{4})(\d{2})(\d{2})", "ymd"),
    "yyMMdd": (r"(\d{2})(\d{2})(\d{2})", "ymd"),
    "MM/dd/yyyy": (r"(\d{2})/(\d{2})/(\d{4})", "mdy"),
    "dd/MM/yyyy": (r"(\d{2})/(\d{2})/(\d{4})", "dmy"),
    "dd.MM.yyyy": (r"(\d{2})\.(\d{2})\.(\d{4})", "dmy"),
}


def _to_date(text: str, fmt: str) -> date:
    pattern, order = _DATE_PATTERNS[fmt]
    parts = dict(zip(order, (int(g) for g in _parse(pattern, text, f"a {fmt} date").groups()), strict=True))
    year = parts["y"] + 2000 if fmt == "yyMMdd" else parts["y"]  # Spark: yy is 2000-2099
    try:
        return date(year, parts["m"], parts["d"])
    except ValueError as exc:
        raise DslEvalError(f"{text!r} is not a real date") from exc


def _to_timestamp(text: str, fmt: str) -> datetime:
    sep = "T" if fmt.startswith("yyyy-MM-dd'T'") else " "
    tail = "Z" if fmt.endswith("'Z'") else ""
    m = _parse(rf"(\d{{4}})-(\d{{2}})-(\d{{2}}){sep}(\d{{2}}):(\d{{2}}):(\d{{2}}){tail}", text, f"a {fmt} timestamp")
    try:
        year, month, day, hour, minute, second = (int(g) for g in m.groups())
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError as exc:
        raise DslEvalError(f"{text!r} is not a real timestamp") from exc


def _date_add(day: date, n: int) -> date:
    try:
        return day + timedelta(days=n)
    except OverflowError as exc:
        raise DslEvalError("the date is out of range") from exc


_STRICT: Final[dict[str, Callable[..., Value]]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "lower": str.lower,
    "upper": str.upper,
    "trim": lambda s: s.strip(" "),
    "normalize_text": lambda s: "".join(ch for ch in s.lower() if ch in _KEEP),
    "length": len,
    "substring": _substring,
    "concat": lambda *parts: "".join(parts),
    "starts_with": str.startswith,
    "ends_with": str.endswith,
    "contains": lambda s, needle: needle in s,
    "regex_extract": _regex_extract,
    "regex_matches": lambda s, p: _compiled(p).search(s) is not None,
    "split_part": _split_part,
    "cast_string": _cast_string,
    "add": lambda a, b: _bigint(a + b),
    "sub": lambda a, b: _bigint(a - b),
    "mul": lambda a, b: _bigint(a * b),
    "neg": lambda a: _bigint(-a),
    "abs": lambda a: _bigint(abs(a)),
    "abs_diff": lambda a, b: _bigint(abs(a - b)),
    "bps_of": lambda amount, bps: _bigint(abs(amount) * bps // 10_000) if bps >= 0 else _negative_bps(),
    "parse_bigint": lambda s: _bigint(int(_parse(r"[+-]?\d{1,19}", s, "an integer").group(0))),
    "parse_decimal": _parse_decimal,
    "parse_decimal_comma": _parse_decimal_comma,
    "parse_implied_decimals": _parse_implied,
    "to_minor_units": _to_minor,
    "currency_exponent": _exponent,
    "to_date": _to_date,
    "to_timestamp": _to_timestamp,
    "date_of": lambda ts: ts.astimezone(UTC).date(),
    "date_add_days": _date_add,
    "days_between": lambda a, b: (b - a).days,
}


def _negative_bps() -> int:
    raise DslEvalError("basis points cannot be negative")
