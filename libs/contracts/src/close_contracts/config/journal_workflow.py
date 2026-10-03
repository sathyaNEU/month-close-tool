"""The journal workflow's configuration (Stage 7, ADR-0016): what the accounting team sets up, as data.

- **Entities.** :class:`EntitySet` lists the legal entities and each one's functional currency.
- **Journal templates.** A :class:`JournalTemplate` is BlackLine's Journal Template. It has typed header and line
  columns (required, read-only with a default, restricted to non-negative values), the accounts its journals may
  use, a line limit, an attachment rule, a minimum description length, and whether its journals reverse into the
  next period.
- **The approval matrix.** :class:`ApprovalMatrix` sets the amount at which each approval level starts, routing
  rules that raise the minimum level, the AS 2401.61 risk-score weights and bands, reminders and escalation, and
  which journals may bypass approval. One is active at a time.
- **Recurring journals.** A :class:`RecurringJournal` is BlackLine's Journal Master: a template, an entity, a
  frequency and day, a preparer, and the lines to create each period.

Column values are strings, as in BlackLine's ``dynamicData``; :func:`value_problem` checks one against its column.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from close_contracts.config.envelope import SLUG
from close_contracts.config.journaling import ACCOUNT_CODE, ENTITY, NAME
from close_contracts.money import CURRENCY_EXPONENTS

CURRENCY: Final = r"^[A-Z]{3}$"
PERIOD: Final = r"^\d{4}-(0[1-9]|1[0-2])$"
AccountType = Literal["asset", "liability", "equity", "revenue", "expense"]
ColumnType = Literal["string", "date", "decimal", "integer", "boolean"]
RiskFactor = Literal[
    "seldom_used_account", "infrequent_preparer", "period_end_thin_description", "sensitive_account", "round_amount"
]
RISK_FACTORS: Final[tuple[RiskFactor, ...]] = (
    "seldom_used_account",
    "infrequent_preparer",
    "period_end_thin_description",
    "sensitive_account",
    "round_amount",
)
# Names a template column can't take, because the journal itself has them.
RESERVED_HEADER: Final = frozenset(
    {"entity_id", "template_id", "accounting_date", "description", "header", "lines", "currency", "number"}
)
RESERVED_LINE: Final = frozenset({"account", "side", "amount_minor", "dimensions", "description", "values", "line_no"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------------------------- entities


class Entity(_Strict):
    entity_id: str = Field(pattern=ENTITY)
    name: str = Field(min_length=1, max_length=200)
    functional_currency: str = Field(pattern=CURRENCY)
    minor_units: int = Field(default=2, ge=0, le=4)  # decimal places of the functional currency


class EntitySet(_Strict):
    entity_set_id: str = Field(pattern=SLUG)
    entities: tuple[Entity, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [e.entity_id for e in self.entities]
        if len(set(ids)) != len(ids):
            raise ValueError("entity ids must be unique")
        return self

    def entity(self, entity_id: str) -> Entity | None:
        return next((e for e in self.entities if e.entity_id == entity_id), None)


# ---------------------------------------------------------------------------------------------- templates


class Column(_Strict):
    """One header or line field of a template (BlackLine's template column)."""

    column_id: str = Field(pattern=NAME)
    label: str = Field(min_length=1, max_length=100)
    data_type: ColumnType = "string"
    required: bool = False
    read_only: bool = False  # the value is always the default
    default: str | None = Field(default=None, max_length=4000)
    allowed_values: tuple[str, ...] = Field(default=(), max_length=500)
    max_length: int = Field(default=500, ge=1, le=4000)
    non_negative: bool = False  # BlackLine's restrictNegativeValues, for numeric columns

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.read_only and self.default is None:
            raise ValueError(f"read-only column {self.column_id} needs a default")
        if self.non_negative and self.data_type not in ("decimal", "integer"):
            raise ValueError(f"only a numeric column can be restricted to non-negative values ({self.column_id})")
        if self.allowed_values and self.data_type != "string":
            raise ValueError(f"only a string column can list allowed values ({self.column_id})")
        for value in (self.default, *self.allowed_values):
            if value is not None and (problem := _type_problem(self, value)):
                raise ValueError(f"column {self.column_id}: {problem}")
        return self


def _type_problem(column: Column, value: str) -> str | None:
    if len(value) > column.max_length:
        return f"is longer than {column.max_length} characters"
    kind = column.data_type
    if kind == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return "is not a date (YYYY-MM-DD)"
    elif kind in ("decimal", "integer"):
        if kind == "integer" and not re.fullmatch(r"-?\d{1,18}", value):
            return "is not an integer"
        try:
            number = Decimal(value)
        except InvalidOperation:
            return "is not a number"
        if not number.is_finite():
            return "is not a finite number"
        if column.non_negative and number < 0:
            return "must not be negative"
    elif kind == "boolean" and value not in ("true", "false"):
        return "is not true or false"
    return None


def value_problem(column: Column, value: str | None) -> str | None:
    """Why ``value`` can't go in ``column`` (None when it can). A missing value is None."""
    if value is None or value == "":
        return "is required" if column.required and column.default is None else None
    if column.read_only and value != column.default:
        return f"is read-only (always {column.default!r})"
    if column.allowed_values and value not in column.allowed_values:
        return f"must be one of {', '.join(column.allowed_values)}"
    return _type_problem(column, value)


class AccountRange(_Strict):
    """Codes from ``first`` to ``last`` inclusive, compared as text (give codes of equal length)."""

    first: str = Field(pattern=ACCOUNT_CODE)
    last: str = Field(pattern=ACCOUNT_CODE)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.first > self.last:
            raise ValueError(f"range {self.first} to {self.last} is backwards")
        return self


class AllowedAccounts(_Strict):
    """The accounts a template's journals may use. Nothing listed means every postable account."""

    account_types: tuple[AccountType, ...] = ()
    ranges: tuple[AccountRange, ...] = Field(default=(), max_length=100)
    codes: tuple[str, ...] = Field(default=(), max_length=1000)

    def allows(self, code: str, account_type: str) -> bool:
        if not (self.account_types or self.ranges or self.codes):
            return True
        return (
            account_type in self.account_types
            or code in self.codes
            or any(r.first <= code <= r.last for r in self.ranges)
        )


class JournalTemplate(_Strict):
    template_id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    header_columns: tuple[Column, ...] = Field(default=(), max_length=40)
    line_columns: tuple[Column, ...] = Field(default=(), max_length=40)
    allowed_accounts: AllowedAccounts = AllowedAccounts()
    max_lines: int = Field(default=500, ge=2, le=10_000)
    attachment_required_from_minor: int | None = Field(default=None, ge=0)  # total debits, functional currency
    min_description_length: int = Field(default=1, ge=1, le=500)
    auto_reverse: bool = False  # an approved journal's reversal is created on the next period's first day
    # Transaction currencies allowed besides the entity's functional currency (Stage 7c); none by default.
    currencies: tuple[str, ...] = Field(default=(), max_length=40)

    @model_validator(mode="after")
    def _currencies(self) -> Self:
        unknown = sorted(c for c in self.currencies if c not in CURRENCY_EXPONENTS)
        if unknown:
            raise ValueError(f"unknown currencies {unknown}")
        if len(set(self.currencies)) != len(self.currencies):
            raise ValueError("currencies must be unique")
        return self

    @model_validator(mode="after")
    def _columns(self) -> Self:
        for kind, columns, reserved in (
            ("header", self.header_columns, RESERVED_HEADER),
            ("line", self.line_columns, RESERVED_LINE),
        ):
            ids = [c.column_id for c in columns]
            if len(set(ids)) != len(ids):
                raise ValueError(f"{kind} column ids must be unique")
            if taken := sorted(set(ids) & reserved):
                raise ValueError(f"{kind} column ids {taken} are reserved")
        return self


# ---------------------------------------------------------------------------------------------- approval matrix


class LevelThresholds(_Strict):
    """Where levels 2 and 3 start, on a journal's total debits in this functional currency (level 1 is the least)."""

    currency: str = Field(pattern=CURRENCY)
    level_2_from_minor: int = Field(ge=1)
    level_3_from_minor: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.level_3_from_minor < self.level_2_from_minor:
            raise ValueError(f"{self.currency}: level 3 must start at or above level 2")
        return self


class RoutingRule(_Strict):
    """Journals matching every listed condition need at least ``min_levels`` levels. An empty list matches all."""

    rule_id: str = Field(pattern=NAME)
    entities: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()  # any line on one of these accounts
    min_levels: int = Field(ge=1, le=3)


class RiskScoring(_Strict):
    """The AS 2401.61 risk score. Each true factor adds its weight, and the bands raise the levels needed.

    The inputs are: (a) an account with no approved line in ``seldom_used_days``; (b) a preparer with fewer than
    ``infrequent_preparer_below`` journals in ``infrequent_preparer_days``; (c) a date in the period's last
    ``period_end_days`` days, or a submission after the period ended, with a description shorter than
    ``thin_description_below``; (d) a line on a sensitive account or on suspense; (e) a line that is a multiple of
    ``round_amount_minor``, or three or more lines sharing their last three digits."""

    version: int = Field(default=1, ge=1)
    weights: dict[RiskFactor, int]
    level_2_from: int = Field(ge=1)
    level_3_from: int = Field(ge=1)
    seldom_used_days: int = Field(default=90, ge=1, le=730)
    infrequent_preparer_below: int = Field(default=3, ge=1, le=1000)
    infrequent_preparer_days: int = Field(default=90, ge=1, le=730)
    period_end_days: int = Field(default=3, ge=0, le=15)
    thin_description_below: int = Field(default=20, ge=1, le=500)
    round_amount_minor: int = Field(default=100_000, ge=100)
    sensitive_accounts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if missing := sorted(set(RISK_FACTORS) - set(self.weights)):
            raise ValueError(f"every risk factor needs a weight; missing {missing}")
        if any(not 0 <= w <= 100 for w in self.weights.values()):
            raise ValueError("risk weights are from 0 to 100")
        if self.level_3_from < self.level_2_from:
            raise ValueError("the level 3 band must start at or above the level 2 band")
        return self


class Reminders(_Strict):
    first_after_hours: int = Field(default=24, ge=1, le=24 * 30)
    repeat_every_hours: int = Field(default=24, ge=1, le=24 * 30)
    escalate_after: int = Field(default=2, ge=1, le=20)  # reminders at one level before escalating


class Bypass(_Strict):
    """Journals that may skip approval. Each one is flagged and listed in the exception report."""

    recurring: bool = False  # a recurring journal marked auto_approve
    reversals: bool = False  # the automatic reversal of an approved auto-reversing journal
    up_to_minor: dict[str, int] = {}  # per functional currency; a journal above its cap is routed normally


class ApprovalMatrix(_Strict):
    matrix_id: str = Field(pattern=SLUG)
    thresholds: tuple[LevelThresholds, ...] = Field(min_length=1)
    rules: tuple[RoutingRule, ...] = Field(default=(), max_length=200)
    risk: RiskScoring
    reminders: Reminders = Reminders()
    bypass: Bypass = Bypass()

    @model_validator(mode="after")
    def _unique(self) -> Self:
        currencies = [t.currency for t in self.thresholds]
        if len(set(currencies)) != len(currencies):
            raise ValueError("one threshold row per currency")
        rules = [r.rule_id for r in self.rules]
        if len(set(rules)) != len(rules):
            raise ValueError("routing rule ids must be unique")
        return self

    def thresholds_for(self, currency: str) -> LevelThresholds | None:
        return next((t for t in self.thresholds if t.currency == currency), None)


# ---------------------------------------------------------------------------------------------- recurring journals


class RecurringLine(_Strict):
    account: str = Field(pattern=ACCOUNT_CODE)
    side: Literal["DR", "CR"]
    amount_minor: int = Field(gt=0)
    dimensions: dict[str, str] = {}
    description: str = Field(default="", max_length=500)
    values: dict[str, str] = {}  # the template's line columns


class RecurringJournal(_Strict):
    """Created in each due period: monthly, or quarterly in the first month of each quarter."""

    recurring_id: str = Field(pattern=SLUG)
    template_id: str = Field(pattern=SLUG)
    entity_id: str = Field(pattern=ENTITY)
    frequency: Literal["monthly", "quarterly"]
    day: int = Field(ge=1, le=28)
    first_period: str = Field(pattern=PERIOD)
    last_period: str | None = Field(default=None, pattern=PERIOD)
    preparer: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=500)
    header: dict[str, str] = {}
    lines: tuple[RecurringLine, ...] = Field(min_length=2, max_length=500)
    auto_approve: bool = False  # only when the approval matrix lets recurring journals bypass approval
    currency: str | None = Field(default=None, pattern=CURRENCY)  # Stage 7c: None is the functional currency

    @model_validator(mode="after")
    def _balanced(self) -> Self:
        debits = sum(ln.amount_minor for ln in self.lines if ln.side == "DR")
        credits = sum(ln.amount_minor for ln in self.lines if ln.side == "CR")
        if debits != credits:
            raise ValueError(f"the lines don't balance: debits {debits}, credits {credits}")
        if self.last_period is not None and self.last_period < self.first_period:
            raise ValueError("last_period is before first_period")
        return self

    def due(self, period: str) -> bool:
        """Whether a journal is due in ``period`` (YYYY-MM)."""
        if period < self.first_period or (self.last_period is not None and period > self.last_period):
            return False
        return self.frequency == "monthly" or int(period[5:]) % 3 == 1
