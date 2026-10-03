"""Data sources (BlackLine's Import Definition + Data Source): how one landing table becomes items.

A data source names a landing table, the columns that identify a source row (so a corrected row becomes
a new version of the same item, and a duplicate row stays visible as a duplicate), a mapping from each
canonical item field to a DSL expression over the landing columns, an optional filter for the rows in
scope, the sign convention, and how new rows are found (Delta change data feed, or a declared column
with a lookback). Your real landing tables plug in by writing their own data source; the expressions
are typechecked against the landing and canonical schemas when the source is bound (Stage 2b/3).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from close_contracts.config.envelope import SLUG
from close_contracts.dsl.ast import COLUMN_PATTERN, Expr

LANDING_TABLE: Final = r"^landing\.[a-z][a-z0-9_]{0,62}$"
FIELD: Final = r"^[a-z][a-z0-9_]{0,62}$"


class SignConvention(StrEnum):
    AS_IS = "as_is"  # amounts already carry the platform's sign
    NEGATE = "negate"  # the source reports the opposite sign


class Incremental(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["cdf", "column"]
    column: str | None = Field(default=None, pattern=COLUMN_PATTERN)
    lookback_days: int = Field(default=3, ge=0, le=90)

    @model_validator(mode="after")
    def _column(self) -> Self:
        if (self.mode == "column") != (self.column is not None):
            raise ValueError("a column is required for mode 'column', and only there")
        return self


class DataSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=SLUG)
    description: str = Field(default="", max_length=500)
    landing_table: str = Field(pattern=LANDING_TABLE)
    identity_key: tuple[str, ...] = Field(min_length=1, max_length=8)
    mapping: dict[str, Expr] = Field(min_length=1, max_length=64)
    filter: Expr | None = None
    sign: SignConvention = SignConvention.AS_IS
    incremental: Incremental = Incremental(mode="cdf")

    @model_validator(mode="after")
    def _names(self) -> Self:
        bad = [c for c in self.identity_key if not re.match(COLUMN_PATTERN, c) or "." in c]
        if bad:
            raise ValueError(f"identity key columns must be plain landing column names: {bad}")
        if len(set(self.identity_key)) != len(self.identity_key):
            raise ValueError("identity key columns must be distinct")
        fields = [f for f in self.mapping if not re.match(FIELD, f)]
        if fields:
            raise ValueError(f"mapping keys must be canonical field names: {fields}")
        return self
