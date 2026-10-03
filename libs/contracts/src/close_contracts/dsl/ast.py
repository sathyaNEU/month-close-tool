"""The rule DSL's JSON form: a typed expression tree (roadmap D10), never a string that gets parsed.

An expression is one of three JSON objects:

* ``{"col": "amount_minor"}`` reads a column. In a pass rule the name carries the side's role, as in
  ``{"col": "a.amount_minor"}``; in a data-source mapping it is the landing table's own column name.
* ``{"lit": 1250, "type": "bigint"}`` is a typed literal. Dates, timestamps and decimals are written as
  strings (``"2026-09-30"``, ``"2026-09-30T12:00:00Z"``, ``"0.0125"``) so they stay exact in JSON.
* ``{"fn": "eq", "args": [ ... ]}`` calls a function from the catalog (``close_contracts.dsl.functions``).

The typecheck (``close_contracts.dsl.typecheck``) turns this tree into the IR, which the Spark
compiler (Stage 3) and the Python interpreter both run.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Discriminator, Field, StrictBool, StrictInt, StrictStr, Tag, TypeAdapter

from close_contracts.canonical_json import MAX_SAFE_INTEGER

COLUMN_PATTERN: Final = r"^(?:[abc]\.)?[A-Za-z_][A-Za-z0-9_]{0,127}$"
FUNCTION_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
MAX_ARGS: Final = 64
MAX_STRING_LITERAL: Final = 1024


class DslType(StrEnum):
    STRING = "string"
    BIGINT = "bigint"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    DECIMAL = "decimal"  # DECIMAL(38,12) in Spark, decimal.Decimal in Python


class Col(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    col: str = Field(pattern=COLUMN_PATTERN)


class Lit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Strict, so JSON 1 stays an integer and never becomes True. Integers are limited to the JSON-safe
    # range (RFC 8785 cannot hash larger ones); runtime arithmetic still has the full BIGINT range.
    lit: StrictBool | Annotated[StrictInt, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)] | StrictStr | None
    type: DslType


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fn: str = Field(pattern=FUNCTION_PATTERN)
    args: tuple[Expr, ...] = Field(default=(), max_length=MAX_ARGS)


def _kind(value: Any) -> str | None:
    if isinstance(value, dict):
        present = {"col", "lit", "fn"} & value.keys()
        return next(iter(present)) if len(present) == 1 else None
    if isinstance(value, Col):
        return "col"
    if isinstance(value, Lit):
        return "lit"
    if isinstance(value, Call):
        return "fn"
    return None


Expr = Annotated[
    Annotated[Col, Tag("col")] | Annotated[Lit, Tag("lit")] | Annotated[Call, Tag("fn")],
    Discriminator(
        _kind, custom_error_type="dsl_node", custom_error_message="an expression needs exactly one of col, lit or fn"
    ),
]
Call.model_rebuild()
EXPR_ADAPTER: Final[TypeAdapter[Col | Lit | Call]] = TypeAdapter(Expr)


def parse(value: Any) -> Col | Lit | Call:
    """Validate JSON (already decoded) into an expression tree; raises pydantic.ValidationError."""
    return EXPR_ADAPTER.validate_python(value)


def dump(expr: Col | Lit | Call) -> Any:
    """The expression as plain JSON data (the inverse of :func:`parse`)."""
    return EXPR_ADAPTER.dump_python(expr, mode="json")
