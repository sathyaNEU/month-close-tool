"""Posting one accounting event: its automated journal definitions become journal lines (ADR-0015).

**Rules.** A rule sees the event's fields, its ``event_type``, and the policy switches (``policy_*``) for the
event's program. Every definition that covers the event type and whose condition holds contributes its lines
whose own condition holds. A zero amount writes no line.

**Data problems go to suspense**, recording the intended account (or the mapping key that found no account)
and the reason:
- an amount that comes out negative;
- a mapping with no row and no default for its keys;
- a missing or unknown required dimension value;
- an event that breaks one of its type's identities, whose difference is balanced to suspense.

A negative amount keeps its economic effect: it posts the absolute amount on the opposite side, to suspense.

**Configuration problems fail the event type** (:class:`ConfigError`):
- an event whose identities hold but whose lines do not balance;
- a rule that posts to an account the chart does not allow for that entity or currency.

**FX (D9).** Each line is converted to the entity's functional currency at the daily spot rate of the accounting
date, exactly, then rounded half-even. The residual goes on one rounding line per event, to the chart's FX
rounding account. A missing rate is a :class:`ReferenceDataError`, which fails the run.

**Periods.** An event dated in a closed period posts to the first open period after it, flagged as a
prior-period adjustment with its original period. A period with no status is open.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from close_contracts.config.accounting_policy import AccountingPolicy
from close_contracts.config.journal_validate import field_types, rule_schema
from close_contracts.config.journaling import (
    Account,
    AccountMapping,
    AutoJournalDefinition,
    ChartOfAccounts,
    DimensionSet,
    EventType,
    LineTemplate,
)
from close_contracts.dsl import DslType, Ir, evaluate, typecheck
from close_contracts.ids import hash_id
from close_engine.reference.journal.events import Event


class ConfigError(Exception):
    """A configuration problem: the event type fails outright in this run."""


class ReferenceDataError(Exception):
    """Reference data is incomplete (a missing FX rate or open period): the run fails."""


@dataclass(frozen=True)
class JournalConfig:
    policy: AccountingPolicy
    chart: ChartOfAccounts
    dimensions: DimensionSet
    mappings: Mapping[str, AccountMapping]
    event_types: Mapping[str, EventType]  # by code
    definitions: Sequence[AutoJournalDefinition]


@dataclass(frozen=True)
class ReferenceData:
    functional: Mapping[str, str]  # entity -> functional currency
    exponents: Mapping[str, int]  # currency -> minor-unit exponent
    rates: Mapping[tuple[date, str, str], tuple[Decimal, int]]  # (date, from, to) -> (daily spot rate, version)
    closed: Mapping[str, frozenset[str]]  # entity -> closed periods (YYYY-MM)


@dataclass(frozen=True)
class Line:
    line_id: str
    event_id: str
    event_version: int
    definition_id: str
    template: str  # the definition's line id, or "suspense" / "rounding"
    entity_id: str
    accounting_date: date
    period: str
    account: str
    side: str
    amount_minor: int
    currency: str
    functional_amount_minor: int
    functional_currency: str
    fx_rate: Decimal | None
    fx_rate_date: date | None
    fx_rate_version: int | None
    dimensions: tuple[tuple[str, str], ...]
    description: str
    is_suspense: bool = False
    intended_account: str | None = None
    reason: str | None = None
    is_prior_period_adjustment: bool = False
    original_period: str | None = None
    is_reversal: bool = False


@dataclass(frozen=True)
class _LineRule:
    template: LineTemplate
    condition: Ir | None
    amount: Ir
    keys: tuple[Ir, ...]
    dimensions: tuple[tuple[str, Ir], ...]
    description: Ir | None


@dataclass(frozen=True)
class _DefinitionRule:
    definition: AutoJournalDefinition
    condition: Ir | None
    lines: tuple[_LineRule, ...]


@dataclass(frozen=True)
class CompiledRules:
    definitions: Mapping[str, tuple[_DefinitionRule, ...]]  # by event type code
    identities: Mapping[str, tuple[tuple[str, Ir], ...]]


def compile_rules(config: JournalConfig) -> CompiledRules:
    definitions: dict[str, list[_DefinitionRule]] = {code: [] for code in config.event_types}
    for d in config.definitions:
        for code in d.event_types:
            schema = rule_schema(config.event_types[code])

            def tc(expr: Any, expect: DslType | None = None, schema: dict[str, DslType] = schema) -> Ir:
                return typecheck(expr, schema, expect=expect)

            lines = tuple(
                _LineRule(
                    t,
                    tc(t.condition, DslType.BOOLEAN) if t.condition is not None else None,
                    tc(t.amount, DslType.BIGINT),
                    tuple(tc(k, DslType.STRING) for k in t.account.keys),
                    tuple((name, tc(e, DslType.STRING)) for name, e in t.dimensions.items()),
                    tc(t.description, DslType.STRING) if t.description is not None else None,
                )
                for t in d.lines
            )
            condition = tc(d.condition, DslType.BOOLEAN) if d.condition is not None else None
            definitions[code].append(_DefinitionRule(d, condition, lines))
    identities = {
        code: tuple((i.name, typecheck(i.expr, field_types(et), expect=DslType.BOOLEAN)) for i in et.identities)
        for code, et in config.event_types.items()
    }
    return CompiledRules({k: tuple(v) for k, v in definitions.items()}, identities)


def policy_fields(policy: AccountingPolicy, program: str | None) -> dict[str, str | None]:
    p = next((x for x in policy.programs if x.program.value == program), None)
    pres = policy.presentation
    return {
        "policy_trigger": p.trigger.value if p else None,
        "policy_model": p.model.value if p else None,
        "policy_shipping": p.shipping.value if p else None,
        "policy_incentives": p.incentives.value if p else None,
        "policy_psp_receivable": pres.psp_receivable,
        "policy_processing_fees": pres.processing_fees,
        "policy_chargebacks": pres.chargebacks,
    }


@dataclass
class _Draft:
    definition_id: str
    template: str
    side: str
    amount: int
    account: str
    dimensions: tuple[tuple[str, str], ...]
    description: str
    intended: str | None = None
    reason: str | None = None


@dataclass
class Posted:
    lines: list[Line] = field(default_factory=list)
    status: str = "no_entry"  # posted, suspended or no_entry


def _required(dimensions: DimensionSet, account: Account) -> list[str]:
    return [
        d.code
        for d in dimensions.dimensions
        if account.account_type in d.required_on or account.code in d.required_accounts
    ]


def _dimension_problem(config: JournalConfig, account: Account, dims: dict[str, str | None]) -> str | None:
    for code in _required(config.dimensions, account):
        if dims.get(code) in (None, ""):
            return f"dimension {code} is required on {account.code}"
    for d in config.dimensions.dimensions:
        value = dims.get(d.code)
        if value is not None and not d.open_values and value not in {v.code for v in d.values if v.status == "active"}:
            return f"{value} is not an active {d.code} value"
    return None


def _account_rule(config: JournalConfig, code: str, entity: str, currency: str, what: str) -> Account:
    """A literal or mapped account that configuration allows; a :class:`ConfigError` otherwise."""
    account = config.chart.account(code)
    if account is None or account.status != "active" or not account.postable:
        raise ConfigError(f"{what}: account {code} is not an active, postable account")
    if account.entities and entity not in account.entities:
        raise ConfigError(f"{what}: account {code} is not open to {entity}")
    if account.currency is not None and account.currency != currency:
        raise ConfigError(f"{what}: account {code} only takes {account.currency}")
    return account


def _resolve(config: JournalConfig, rule: _LineRule, row: Mapping[str, Any]) -> tuple[str | None, str]:
    """(account, intended): account None means no account was found (a data problem)."""
    ref = rule.template.account
    if ref.account is not None:
        return ref.account, ref.account
    keys = [evaluate(k, row) for k in rule.keys]
    mapping = config.mappings[str(ref.mapping)]
    intended = f"mapping:{mapping.mapping_id}:{'|'.join('' if k is None else str(k) for k in keys)}"
    if any(k is None for k in keys):
        return None, intended
    found = next((r.account for r in mapping.rows if list(r.match) == keys), mapping.default_account)
    return found, intended


def _drafts(
    config: JournalConfig, rules: CompiledRules, event: Event, row: dict[str, Any]
) -> tuple[list[_Draft], bool]:
    """The event's lines before FX and periods, and whether an amount was null (incomplete data)."""
    entity, currency = str(event.fields["entity_id"]), str(event.fields["currency"])
    drafts: list[_Draft] = []
    incomplete = False
    suspense = config.chart.suspense_account
    for d in rules.definitions.get(event.event_type, ()):
        if d.condition is not None and evaluate(d.condition, row) is not True:
            continue
        for rule in d.lines:
            if rule.condition is not None and evaluate(rule.condition, row) is not True:
                continue
            value = evaluate(rule.amount, row)
            if not isinstance(value, int) or isinstance(value, bool):
                incomplete = True  # a null amount: the event's data is incomplete
                continue
            if value == 0:
                continue
            what = f"{d.definition.definition_id}/{rule.template.line_id}"
            dims: dict[str, str | None] = {}
            for name, ir in rule.dimensions:
                v = evaluate(ir, row)
                dims[name] = None if v is None else str(v)
            dim_tuple = tuple(sorted((k, v) for k, v in dims.items() if v is not None))
            text = evaluate(rule.description, row) if rule.description is not None else d.definition.name
            draft = _Draft(
                d.definition.definition_id, rule.template.line_id, rule.template.side, value, "", dim_tuple, str(text)
            )
            code, intended = _resolve(config, rule, row)
            if code is None:
                draft.account, draft.intended, draft.reason = suspense, intended, "no account for the mapping keys"
            else:
                account = _account_rule(config, code, entity, currency, what)
                problem = _dimension_problem(config, account, dims)
                if problem is not None:
                    draft.account, draft.intended, draft.reason = suspense, code, problem
                else:
                    draft.account = code
            if draft.amount < 0:
                draft.amount, draft.side = -draft.amount, {"DR": "CR", "CR": "DR"}[draft.side]
                draft.intended = draft.intended or draft.account
                draft.account, draft.reason = suspense, "negative amount"
            drafts.append(draft)
    return drafts, incomplete


def _functional(
    ref: ReferenceData, amount: int, currency: str, functional: str, day: date
) -> tuple[int, Decimal | None, int | None]:
    if currency == functional:
        return amount, None, None
    found = ref.rates.get((day, currency, functional))
    if found is None:
        raise ReferenceDataError(f"no daily_spot rate for {day} {currency}->{functional}")
    rate, version = found
    shift = Decimal(10) ** (ref.exponents.get(functional, 2) - ref.exponents.get(currency, 2))
    value = (Decimal(amount) * rate * shift).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return int(value), rate, version


def _period(ref: ReferenceData, entity: str, day: date) -> tuple[str, bool, str | None]:
    period = f"{day:%Y-%m}"
    closed = ref.closed.get(entity, frozenset())
    if period not in closed:
        return period, False, None
    year, month = day.year, day.month
    for _ in range(120):
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        candidate = f"{year:04d}-{month:02d}"
        if candidate not in closed:
            return candidate, True, period
    raise ReferenceDataError(f"no open period after {period} for {entity}")


def post(config: JournalConfig, rules: CompiledRules, ref: ReferenceData, event: Event, version: int) -> Posted:
    fields = event.fields
    row = dict(fields) | {"event_type": event.event_type} | policy_fields(config.policy, fields.get("program"))
    broken = [name for name, ir in rules.identities.get(event.event_type, ()) if evaluate(ir, fields) is not True]
    drafts, incomplete = _drafts(config, rules, event, row)
    debit = sum(d.amount for d in drafts if d.side == "DR")
    credit = sum(d.amount for d in drafts if d.side == "CR")
    if debit != credit:
        if not broken and not incomplete:
            raise ConfigError(f"{event.event_type}: the lines of event {event.event_id} differ by {debit - credit}")
        reason = f"identity broken: {', '.join(broken)}" if broken else "an amount is missing"
        drafts.append(
            _Draft(
                "",
                "suspense",
                "CR" if debit > credit else "DR",
                abs(debit - credit),
                config.chart.suspense_account,
                (),
                reason,
                None,
                reason,
            )
        )
    if not drafts:
        return Posted()
    entity, currency = str(fields["entity_id"]), str(fields["currency"])
    day: date = fields["accounting_date"]
    functional = ref.functional[entity]
    period, ppa, original = _period(ref, entity, day)
    lines: list[Line] = []
    for d in drafts:
        amount_f, rate, rate_version = _functional(ref, d.amount, currency, functional, day)
        lines.append(
            Line(
                line_id=hash_id("line", event.event_id, str(version), d.definition_id, d.template),
                event_id=event.event_id,
                event_version=version,
                definition_id=d.definition_id,
                template=d.template,
                entity_id=entity,
                accounting_date=day,
                period=period,
                account=d.account,
                side=d.side,
                amount_minor=d.amount,
                currency=currency,
                functional_amount_minor=amount_f,
                functional_currency=functional,
                fx_rate=rate,
                fx_rate_date=day if rate is not None else None,
                fx_rate_version=rate_version,
                dimensions=d.dimensions,
                description=d.description,
                is_suspense=d.account == config.chart.suspense_account,
                intended_account=d.intended,
                reason=d.reason,
                is_prior_period_adjustment=ppa,
                original_period=original,
            )
        )
    residual = sum(ln.functional_amount_minor for ln in lines if ln.side == "DR") - sum(
        ln.functional_amount_minor for ln in lines if ln.side == "CR"
    )
    if residual:
        lines.append(
            Line(
                line_id=hash_id("line", event.event_id, str(version), "", "rounding"),
                event_id=event.event_id,
                event_version=version,
                definition_id="",
                template="rounding",
                entity_id=entity,
                accounting_date=day,
                period=period,
                account=config.chart.fx_rounding_account,
                side="CR" if residual > 0 else "DR",
                amount_minor=0,
                currency=currency,
                functional_amount_minor=abs(residual),
                functional_currency=functional,
                fx_rate=None,
                fx_rate_date=None,
                fx_rate_version=None,
                dimensions=(),
                description="FX rounding",
                is_prior_period_adjustment=ppa,
                original_period=original,
            )
        )
    status = "suspended" if any(ln.is_suspense for ln in lines) else "posted"
    return Posted(lines, status)
