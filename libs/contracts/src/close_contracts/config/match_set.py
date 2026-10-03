"""Match sets and pass rules (roadmap "Engine design", Transaction Matching).

A match set binds one to three data sources in roles ``a``, ``b`` and ``c`` and runs an ordered
waterfall of pass rules. A pass rule has a kind (``1:1``, ``1:M``, ``M:1``, ``M:M``, ``self_net`` or
``chain``), typed conditions whose operands are DSL expressions over the sides' item fields (``a.``,
``b.``), an outcome (``auto`` or ``suggested``), an ambiguity policy, and hard limits on the work one
rule may do. The validator enforces the roadmap's rules: an ``M:M`` rule needs an exact key, and every
rule must be able to block (an exact key, or a date window together with an amount condition).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from close_contracts.canonical_json import MAX_SAFE_INTEGER
from close_contracts.config.envelope import PRINCIPAL, SLUG
from close_contracts.dsl import regex
from close_contracts.dsl.ast import Expr

CALENDAR: Final = r"^[A-Z][A-Z0-9_]{1,31}$"
ENTITY: Final = r"^[A-Z0-9]{2,16}$"


class Role(StrEnum):
    A = "a"
    B = "b"
    C = "c"


class RuleKind(StrEnum):
    ONE_TO_ONE = "1:1"
    ONE_TO_MANY = "1:M"
    MANY_TO_ONE = "M:1"
    MANY_TO_MANY = "M:M"
    SELF_NET = "self_net"
    CHAIN = "chain"


class Outcome(StrEnum):
    AUTO = "auto"
    SUGGESTED = "suggested"


class Ambiguity(StrEnum):
    UNIQUE_ONLY = "unique_only"
    BEST_UNIQUE = "best_unique"
    GREEDY_TOTAL_ORDER = "greedy_total_order"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KeyEquals(_Strict):
    """An exact key: the rule blocks on it with an equi-join."""

    condition: Literal["key_equals"] = "key_equals"
    left: Expr
    right: Expr


class TextMatch(_Strict):
    condition: Literal["text"] = "text"
    op: Literal["equals", "normalized_equals", "contains", "starts_with", "ends_with", "regex_extract_equals", "fuzzy"]
    left: Expr
    right: Expr
    min_needle_length: int = Field(default=4, ge=1, le=256)  # contains, starts_with, ends_with
    pattern: str | None = None  # regex_extract_equals: applied to both sides
    group: int = Field(default=1, ge=0, le=9)
    algorithm: Literal["levenshtein", "jaro_winkler"] | None = None  # fuzzy
    min_similarity_bps: int | None = Field(default=None, ge=0, le=10_000)  # fuzzy, quantized to basis points

    @model_validator(mode="after")
    def _op_fields(self) -> Self:
        if (self.op == "regex_extract_equals") != (self.pattern is not None):
            raise ValueError("a pattern is required for regex_extract_equals, and only there")
        if self.pattern is not None:
            compiled = regex.validate(self.pattern)
            if self.group > compiled.groups:
                raise ValueError(f"the pattern has {compiled.groups} groups")
        fuzzy = self.algorithm is not None or self.min_similarity_bps is not None
        if (self.op == "fuzzy") != fuzzy or (fuzzy and (self.algorithm is None or self.min_similarity_bps is None)):
            raise ValueError("fuzzy needs both algorithm and min_similarity_bps, and only fuzzy takes them")
        return self


class DateWindow(_Strict):
    """``right`` within [left - before, left + after], in calendar or business days (asymmetric allowed)."""

    condition: Literal["date_window"] = "date_window"
    left: Expr
    right: Expr
    unit: Literal["calendar_days", "business_days"]
    before: int = Field(ge=0, le=366)
    after: int = Field(ge=0, le=366)
    calendar: str | None = Field(default=None, pattern=CALENDAR)

    @model_validator(mode="after")
    def _calendar(self) -> Self:
        if (self.unit == "business_days") != (self.calendar is not None):
            raise ValueError("a calendar is required for business_days, and only there")
        return self


class AmountTolerance(_Strict):
    """Integer arithmetic on minor units: exact, absolute, basis points, or basis points with a cap."""

    condition: Literal["amount"] = "amount"
    left: Expr
    right: Expr
    mode: Literal["exact", "absolute", "bps", "bps_capped"]
    absolute_minor: int = Field(default=0, ge=0, le=MAX_SAFE_INTEGER)
    bps: int = Field(default=0, ge=0, le=10_000)
    cap_minor: int = Field(default=0, ge=0, le=MAX_SAFE_INTEGER)

    @model_validator(mode="after")
    def _mode(self) -> Self:
        used = {"absolute": self.absolute_minor > 0, "bps": self.bps > 0, "cap": self.cap_minor > 0}
        wanted = {
            "exact": set(),
            "absolute": {"absolute"},
            "bps": {"bps"},
            "bps_capped": {"bps", "cap"},
        }[self.mode]
        if {k for k, v in used.items() if v} != wanted:
            raise ValueError(f"mode {self.mode} takes exactly {sorted(wanted) or 'no tolerance fields'}")
        return self


class SignRule(_Strict):
    condition: Literal["sign"] = "sign"
    mode: Literal["same", "opposite", "absolute"]


class CurrencyRule(_Strict):
    condition: Literal["currency"] = "currency"
    mode: Literal["equal", "fx_converted"]
    tolerance_bps: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def _tolerance(self) -> Self:
        if self.mode == "equal" and self.tolerance_bps:
            raise ValueError("equal currencies take no FX tolerance")
        return self


class ExpressionCondition(_Strict):
    """Any boolean DSL expression over both sides, for what the typed conditions do not cover."""

    condition: Literal["expression"] = "expression"
    expr: Expr


Condition = Annotated[
    KeyEquals | TextMatch | DateWindow | AmountTolerance | SignRule | CurrencyRule | ExpressionCondition,
    Field(discriminator="condition"),
]


class Limits(_Strict):
    """Work limits: an item or block over them is never auto-matched (``too_many_candidates``)."""

    max_candidates_per_item: int = Field(default=50, ge=1, le=1000)
    max_block_pairs: int = Field(default=1_000_000, ge=1, le=100_000_000)
    subset_max_candidates: int = Field(default=24, ge=2, le=24)  # 1:M / M:1 meet-in-the-middle
    work_budget: int = Field(default=2**20, ge=1, le=2**24)  # subset-sum evaluations


class PassRule(_Strict):
    rule_id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    kind: RuleKind
    sides: tuple[Role, ...] = Field(min_length=1, max_length=2)
    outcome: Outcome
    ambiguity: Ambiguity = Ambiguity.UNIQUE_ONLY
    allow_ambiguous_auto: bool = False
    conditions: tuple[Condition, ...] = Field(min_length=1, max_length=20)
    group_by: tuple[Expr, ...] = Field(default=(), max_length=8)  # the aggregation key for 1:M and M:1
    limits: Limits = Limits()

    @model_validator(mode="after")
    def _rules(self) -> Self:
        single = self.kind == RuleKind.SELF_NET
        if single != (len(self.sides) == 1) or len(set(self.sides)) != len(self.sides):
            raise ValueError("self_net rules have one side; every other kind has two different sides")
        keys = any(isinstance(c, KeyEquals) for c in self.conditions)
        window = any(isinstance(c, DateWindow) for c in self.conditions)
        amount = any(isinstance(c, AmountTolerance) for c in self.conditions)
        if self.kind == RuleKind.MANY_TO_MANY and not keys:
            raise ValueError("an M:M rule needs an exact key (key_equals)")
        if not single and not keys and not (window and amount):
            raise ValueError("a rule must be able to block: an exact key, or a date window with an amount condition")
        if self.kind in {RuleKind.ONE_TO_MANY, RuleKind.MANY_TO_ONE} and not self.group_by:
            raise ValueError("1:M and M:1 rules need a group_by key")
        if self.allow_ambiguous_auto and (
            self.outcome != Outcome.AUTO or self.ambiguity != Ambiguity.GREEDY_TOTAL_ORDER
        ):
            raise ValueError("allow_ambiguous_auto only applies to auto rules with greedy_total_order")
        return self


class TimingRule(_Strict):
    """Classifies an unmatched item as in transit, expected to clear within N business days."""

    rule_id: str = Field(pattern=SLUG)
    role: Role
    when: Expr
    expected_clear_business_days: int = Field(ge=0, le=60)
    calendar: str = Field(pattern=CALENDAR)


class SourceBinding(_Strict):
    role: Role
    source_id: str = Field(pattern=SLUG)
    scope: Expr | None = None  # which of the source's items belong to this match set


class Owners(_Strict):
    preparer: str = Field(pattern=PRINCIPAL)
    reviewer: str = Field(pattern=PRINCIPAL)

    @model_validator(mode="after")
    def _separate(self) -> Self:
        if self.preparer == self.reviewer:
            raise ValueError("the preparer and the reviewer must be different people")
        return self


class Schedule(_Strict):
    mode: Literal["manual", "daily"]
    at_utc: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def _time(self) -> Self:
        if (self.mode == "daily") != (self.at_utc is not None):
            raise ValueError("a daily schedule needs at_utc (HH:MM), and only a daily one")
        return self


class MatchSet(_Strict):
    match_set_id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["matching", "intercompany"] = "matching"
    entity_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    sources: tuple[SourceBinding, ...] = Field(min_length=1, max_length=3)
    pass_rules: tuple[PassRule, ...] = Field(min_length=1, max_length=50)
    timing_rules: tuple[TimingRule, ...] = Field(default=(), max_length=20)
    owners: Owners
    schedule: Schedule = Schedule(mode="manual")

    @model_validator(mode="after")
    def _references(self) -> Self:
        bad_entities = [e for e in self.entity_ids if not re.match(ENTITY, e)]
        if bad_entities or len(set(self.entity_ids)) != len(self.entity_ids):
            raise ValueError(f"entity ids must be distinct codes like US01: {bad_entities}")
        roles = [s.role for s in self.sources]
        if len(set(roles)) != len(roles):
            raise ValueError("each role is bound to one source")
        ids = [r.rule_id for r in self.pass_rules] + [t.rule_id for t in self.timing_rules]
        if len(set(ids)) != len(ids):
            raise ValueError("rule ids must be unique within a match set")
        unbound = sorted({side.value for r in self.pass_rules for side in r.sides} - {r.value for r in roles})
        unbound += sorted({t.role.value for t in self.timing_rules} - {r.value for r in roles})
        if unbound:
            raise ValueError(f"rules use roles with no bound source: {sorted(set(unbound))}")
        return self
