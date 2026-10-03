"""The DSL's intermediate representation: a typed, validated expression tree.

The typecheck produces it from the JSON form, with every literal already converted to its Python value
(``datetime.date``, a UTC ``datetime``, ``decimal.Decimal``) and nested ``and``/``or`` flattened. The
Spark compiler (Stage 3) and the interpreter both consume it, and :func:`fingerprint` gives a stable
content hash for audit and caching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from close_contracts.canonical_json import JSONValue, content_sha256
from close_contracts.dsl.ast import DslType

Value = bool | int | str | date | datetime | Decimal | None


@dataclass(frozen=True)
class IrCol:
    name: str
    type: DslType


@dataclass(frozen=True)
class IrLit:
    value: Value
    type: DslType


@dataclass(frozen=True)
class IrCall:
    fn: str
    args: tuple[Ir, ...]
    type: DslType


Ir = IrCol | IrLit | IrCall


def literal_json(value: Value, type_: DslType) -> JSONValue:
    """A literal value back in its JSON form (the inverse of the typecheck's conversion)."""
    if value is None or (isinstance(value, bool | int) and type_ in {DslType.BOOLEAN, DslType.BIGINT}):
        return value
    if type_ == DslType.DATE and isinstance(value, date):
        return value.isoformat()
    if type_ == DslType.TIMESTAMP and isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if type_ == DslType.DECIMAL and isinstance(value, Decimal):
        return format(value, "f")
    if type_ == DslType.STRING and isinstance(value, str):
        return value
    raise TypeError(f"{value!r} is not a {type_} literal")


def to_json(ir: Ir) -> JSONValue:
    if isinstance(ir, IrCol):
        return {"col": ir.name, "type": ir.type.value}
    if isinstance(ir, IrLit):
        return {"lit": literal_json(ir.value, ir.type), "type": ir.type.value}
    return {"fn": ir.fn, "args": [to_json(a) for a in ir.args], "type": ir.type.value}


def fingerprint(ir: Ir) -> str:
    """SHA-256 of the IR's canonical JSON (RFC 8785)."""
    return content_sha256(to_json(ir))


def columns(ir: Ir) -> frozenset[str]:
    """Every column the expression reads."""
    if isinstance(ir, IrCol):
        return frozenset({ir.name})
    if isinstance(ir, IrLit):
        return frozenset()
    return frozenset().union(*(columns(a) for a in ir.args))
