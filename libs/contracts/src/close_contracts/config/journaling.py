"""Journaling configuration (roadmap "Journaling", ADR-0015): what the accounting team sets up, as data.

- **Event types.** An :class:`EventType` says how landing rows become accounting events: its sources, keyed
  lookups into other landing tables, and its declared fields. Amounts are ``amount_<role>`` in minor units,
  and attributes are ``attr_<name>``. It also declares amount identities that every event must satisfy.
- **Automated journal definitions.** An :class:`AutoJournalDefinition` is BlackLine's Automated Journal
  Definition. It names the event types it journals and an optional condition, and its line templates say
  the side, account, amount, dimensions and description of each journal line. The roadmap's "posting
  rules" are these line templates.
- **Account mappings.** An :class:`AccountMapping` turns key values (a policy switch, an attribute) into an
  account, so a rule can pick its account without hard-coding it.
- **Chart and dimensions.** :class:`ChartOfAccounts` and :class:`DimensionSet` are the reference
  configuration that rules must stay within.

Every expression is the rule DSL (ADR-0011). ``close_contracts.config.journal_validate`` typechecks them
against the landing tables and the declared event fields.
"""

from __future__ import annotations

import re
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from close_contracts.config.data_source import LANDING_TABLE
from close_contracts.config.envelope import SLUG
from close_contracts.dsl.ast import Expr

EVENT_CODE: Final = r"^[A-Z][A-Z0-9_]{2,63}$"
NAME: Final = r"^[a-z][a-z0-9_]{0,40}$"  # an amount role, attribute, alias or dimension code
ACCOUNT_CODE: Final = r"^[A-Za-z0-9._-]{1,32}$"
ENTITY: Final = r"^[A-Z0-9]{2,16}$"
STANDARD_FIELDS: Final = ("entity_id", "currency", "accounting_date", "program", "order_id", "seller_id")
REQUIRED_FIELDS: Final = ("entity_id", "currency", "accounting_date")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Lookup(_Strict):
    """One row of another landing table, found by that table's full identity key."""

    alias: str = Field(pattern=NAME)  # its columns become <alias>_<column>
    table: str = Field(pattern=LANDING_TABLE)
    on: dict[str, Expr] = Field(min_length=1)  # target identity column -> expression over the source row


class EventSource(_Strict):
    source_id: str = Field(pattern=SLUG)
    landing_table: str = Field(pattern=LANDING_TABLE)
    lookups: tuple[Lookup, ...] = Field(default=(), max_length=4)
    filter: Expr | None = None
    mapping: dict[str, Expr]  # event field -> expression over the source row and its lookups

    @model_validator(mode="after")
    def _aliases(self) -> Self:
        aliases = [lk.alias for lk in self.lookups]
        if len(set(aliases)) != len(aliases):
            raise ValueError("each lookup needs its own alias")
        missing = [f for f in REQUIRED_FIELDS if f not in self.mapping]
        if missing:
            raise ValueError(f"every source maps {', '.join(REQUIRED_FIELDS)}; missing {missing}")
        return self


class Identity(_Strict):
    """An equation every event must satisfy. An event that breaks one is a data problem (suspense)."""

    name: str = Field(pattern=NAME)
    expr: Expr  # a boolean expression over the event's fields


class EventType(_Strict):
    type_id: str = Field(pattern=SLUG)  # the configuration id
    event_type: str = Field(pattern=EVENT_CODE)  # the code rules refer to
    description: str = Field(min_length=1, max_length=500)
    amounts: tuple[str, ...] = Field(min_length=1, max_length=40)
    attributes: tuple[str, ...] = Field(default=(), max_length=40)
    identities: tuple[Identity, ...] = Field(default=(), max_length=20)
    sources: tuple[EventSource, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _fields(self) -> Self:
        for names, kind in ((self.amounts, "amount"), (self.attributes, "attribute")):
            bad = [n for n in names if not re.fullmatch(NAME, n)]
            if bad or len(set(names)) != len(names):
                raise ValueError(f"{kind} names must be distinct lower-case names: {bad}")
        declared = set(STANDARD_FIELDS) | {f"amount_{a}" for a in self.amounts} | {f"attr_{a}" for a in self.attributes}
        for source in self.sources:
            unknown = sorted(set(source.mapping) - declared)
            if unknown:
                raise ValueError(f"source {source.source_id} maps undeclared fields {unknown}")
        ids = [s.source_id for s in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("source ids must be unique")
        identity_names = [i.name for i in self.identities]
        if len(set(identity_names)) != len(identity_names):
            raise ValueError("identity names must be unique")
        return self

    def fields(self) -> tuple[str, ...]:
        return (*STANDARD_FIELDS, *(f"amount_{a}" for a in self.amounts), *(f"attr_{a}" for a in self.attributes))


class AccountRef(_Strict):
    """A literal account, or an account looked up in a mapping by key expressions."""

    account: str | None = Field(default=None, pattern=ACCOUNT_CODE)
    mapping: str | None = Field(default=None, pattern=SLUG)
    keys: tuple[Expr, ...] = Field(default=(), max_length=6)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.account is None) == (self.mapping is None):
            raise ValueError("an account reference is either a literal account or a mapping, not both")
        if self.mapping is not None and not self.keys:
            raise ValueError("a mapping reference needs its key expressions")
        if self.account is not None and self.keys:
            raise ValueError("a literal account takes no keys")
        return self


class LineTemplate(_Strict):
    line_id: str = Field(pattern=NAME)
    condition: Expr | None = None  # the line is written only when this holds
    side: Literal["DR", "CR"]
    account: AccountRef
    amount: Expr  # minor units in the event currency; a zero amount writes no line
    dimensions: dict[str, Expr] = Field(default_factory=dict)
    description: Expr | None = None


class AutoJournalDefinition(_Strict):
    definition_id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    event_types: tuple[str, ...] = Field(min_length=1, max_length=20)
    condition: Expr | None = None
    lines: tuple[LineTemplate, ...] = Field(min_length=2, max_length=40)

    @model_validator(mode="after")
    def _lines(self) -> Self:
        bad = [t for t in self.event_types if not re.fullmatch(EVENT_CODE, t)]
        if bad or len(set(self.event_types)) != len(self.event_types):
            raise ValueError(f"event types must be distinct codes: {bad}")
        ids = [line.line_id for line in self.lines]
        if len(set(ids)) != len(ids):
            raise ValueError("line ids must be unique within a definition")
        return self


class MappingRow(_Strict):
    match: tuple[str, ...] = Field(min_length=1, max_length=6)
    account: str = Field(pattern=ACCOUNT_CODE)


class AccountMapping(_Strict):
    mapping_id: str = Field(pattern=SLUG)
    description: str = Field(min_length=1, max_length=500)
    keys: tuple[str, ...] = Field(min_length=1, max_length=6)  # what each key value means, for people
    rows: tuple[MappingRow, ...] = Field(min_length=1, max_length=5000)
    default_account: str | None = Field(default=None, pattern=ACCOUNT_CODE)

    @model_validator(mode="after")
    def _rows(self) -> Self:
        if any(len(r.match) != len(self.keys) for r in self.rows):
            raise ValueError(f"every row matches {len(self.keys)} key values")
        matches = [r.match for r in self.rows]
        if len(set(matches)) != len(matches):
            raise ValueError("each key combination maps to one account")
        return self

    def accounts(self) -> set[str]:
        return {r.account for r in self.rows} | ({self.default_account} if self.default_account else set())


class Account(_Strict):
    code: str = Field(pattern=ACCOUNT_CODE)
    name: str = Field(min_length=1, max_length=200)
    account_type: Literal["asset", "liability", "equity", "revenue", "expense"]
    normal_balance: Literal["debit", "credit"]
    postable: bool = True
    parent: str | None = Field(default=None, pattern=ACCOUNT_CODE)
    entities: tuple[str, ...] = ()  # empty means every entity
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    status: Literal["active", "inactive"] = "active"
    description: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _entities(self) -> Self:
        if any(not re.fullmatch(ENTITY, e) for e in self.entities):
            raise ValueError("entities are codes like US01")
        return self


class ChartOfAccounts(_Strict):
    chart_id: str = Field(pattern=SLUG)
    accounts: tuple[Account, ...] = Field(min_length=1, max_length=20000)
    suspense_account: str = Field(pattern=ACCOUNT_CODE)
    fx_rounding_account: str = Field(pattern=ACCOUNT_CODE)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        codes = [a.code for a in self.accounts]
        if len(set(codes)) != len(codes):
            raise ValueError("account codes must be unique")
        by_code = {a.code: a for a in self.accounts}
        missing_parents = sorted({a.parent for a in self.accounts if a.parent and a.parent not in by_code})
        if missing_parents:
            raise ValueError(f"parent accounts not in the chart: {missing_parents}")
        for role in ("suspense_account", "fx_rounding_account"):
            code = getattr(self, role)
            account = by_code.get(code)
            if account is None or not account.postable or account.status != "active":
                raise ValueError(f"{role} {code} must be an active, postable account of the chart")
        return self

    def account(self, code: str) -> Account | None:
        return next((a for a in self.accounts if a.code == code), None)


class DimensionValue(_Strict):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "inactive"] = "active"


class Dimension(_Strict):
    code: str = Field(pattern=NAME)
    name: str = Field(min_length=1, max_length=200)
    required_on: tuple[Literal["asset", "liability", "equity", "revenue", "expense"], ...] = ()
    required_accounts: tuple[str, ...] = ()
    open_values: bool = False  # any value is accepted (for example seller ids)
    values: tuple[DimensionValue, ...] = ()

    @model_validator(mode="after")
    def _values(self) -> Self:
        if not self.open_values and not self.values:
            raise ValueError("a closed dimension lists its values")
        codes = [v.code for v in self.values]
        if len(set(codes)) != len(codes):
            raise ValueError("dimension value codes must be unique")
        return self


class DimensionSet(_Strict):
    dimension_set_id: str = Field(pattern=SLUG)
    dimensions: tuple[Dimension, ...] = Field(max_length=20)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        codes = [d.code for d in self.dimensions]
        if len(set(codes)) != len(codes):
            raise ValueError("dimension codes must be unique")
        return self
