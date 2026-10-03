"""Manual journals (Stage 7, ADR-0016): the body a preparer sends, and the pure checks the app runs on it.

- :class:`JournalDraft` is a journal's content: entity, template, date, description, header values and lines.
  Its amounts are in its own currency: the entity's functional currency unless it names another (Stage 7c).
- :func:`journal_issues` checks a draft against its template, the chart of accounts and the dimension set.
- :func:`content_hash` is what an approval is bound to.
- :func:`convert` converts a foreign-currency draft to the functional currency under D9, and
  :class:`Conversion` is what a submission freezes and its approvals are bound to (ADR-0018).
- :func:`risk_score` scores a draft against AS 2401.61's risk characteristics, from inputs the app gathers.
- :func:`route` decides how many approval levels a draft needs: the most that its amount, its risk band or a
  routing rule asks for.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from close_contracts.canonical_json import content_sha256
from close_contracts.config.envelope import SLUG
from close_contracts.config.journal_workflow import (
    RISK_FACTORS,
    ApprovalMatrix,
    JournalTemplate,
    RecurringJournal,
    RiskScoring,
    value_problem,
)
from close_contracts.config.journaling import ACCOUNT_CODE, ENTITY, ChartOfAccounts, DimensionSet
from close_contracts.dsl import Issue
from close_contracts.fx import Rate
from close_contracts.money import CURRENCY_EXPONENTS, convert_minor

MAX_AMOUNT_MINOR: Final = 10**15


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JournalLineIn(_Strict):
    account: str = Field(pattern=ACCOUNT_CODE)
    side: Literal["DR", "CR"]
    amount_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR)
    dimensions: dict[str, str] = {}
    description: str = Field(default="", max_length=500)
    values: dict[str, str] = {}  # the template's line columns


class JournalDraft(_Strict):
    entity_id: str = Field(pattern=ENTITY)
    template_id: str = Field(pattern=SLUG)
    accounting_date: date
    description: str = Field(default="", max_length=1000)
    header: dict[str, str] = {}
    lines: tuple[JournalLineIn, ...] = Field(min_length=1, max_length=10_000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")  # None: the entity's functional currency

    def total_debits(self) -> int:
        return sum(ln.amount_minor for ln in self.lines if ln.side == "DR")

    def total_credits(self) -> int:
        return sum(ln.amount_minor for ln in self.lines if ln.side == "CR")


def content_hash(draft: JournalDraft) -> str:
    """SHA-256 of the draft's canonical JSON (RFC 8785): the content an approval is bound to.

    An unset currency is left out, so a functional-currency draft hashes exactly as it did before Stage 7c and
    journals already in approval keep the content their approvals are bound to."""
    data: Any = draft.model_dump(mode="json", exclude={"currency"} if draft.currency is None else None)
    return content_sha256(data)


def in_functional(draft: JournalDraft, functional: str) -> JournalDraft:
    """The same draft, with a currency equal to the functional one stored as unset (the canonical form)."""
    return draft.model_copy(update={"currency": None}) if draft.currency == functional else draft


def from_recurring(recurring: RecurringJournal, accounting_date: date) -> JournalDraft:
    return JournalDraft(
        entity_id=recurring.entity_id,
        template_id=recurring.template_id,
        accounting_date=accounting_date,
        description=recurring.description,
        header=recurring.header,
        currency=recurring.currency,
        lines=tuple(
            JournalLineIn(
                account=ln.account,
                side=ln.side,
                amount_minor=ln.amount_minor,
                dimensions=ln.dimensions,
                description=ln.description,
                values=ln.values,
            )
            for ln in recurring.lines
        ),
    )


def _flipped(draft: JournalDraft) -> tuple[JournalLineIn, ...]:
    return tuple(ln.model_copy(update={"side": "CR" if ln.side == "DR" else "DR"}) for ln in draft.lines)


def reversal_of(draft: JournalDraft, accounting_date: date, description: str) -> JournalDraft:
    """The same lines on the other side, in the same currency, dated ``accounting_date``."""
    return draft.model_copy(
        update={"accounting_date": accounting_date, "description": description, "lines": _flipped(draft)}
    )


def reverses_exactly(reversal: JournalDraft, original: JournalDraft) -> bool:
    """Whether ``reversal`` is still the plain flip of ``original``: the same currency and the same lines on the
    other side. Only then does it reuse the original's rate (ADR-0018); an edited reversal converts afresh."""
    return reversal.currency == original.currency and reversal.lines == _flipped(original)


# ---------------------------------------------------------------------------------------------- validation


def _dimension_issues(
    path: str, dims: dict[str, str], code: str, account_type: str, dimensions: DimensionSet | None
) -> list[Issue]:
    issues: list[Issue] = []
    known = {d.code: d for d in dimensions.dimensions} if dimensions is not None else {}
    for name, value in sorted(dims.items()):
        dimension = known.get(name)
        if dimension is None:
            issues.append(Issue("unknown_dimension", f"{path}/dimensions/{name}", f"dimension {name} does not exist"))
        elif not dimension.open_values and not any(v.code == value and v.status == "active" for v in dimension.values):
            issues.append(
                Issue("invalid_dimension_value", f"{path}/dimensions/{name}", f"{value!r} is not an active {name}")
            )
    for dimension in known.values():
        needed = account_type in dimension.required_on or code in dimension.required_accounts
        if needed and not dims.get(dimension.code):
            issues.append(
                Issue(
                    "missing_dimension",
                    f"{path}/dimensions/{dimension.code}",
                    f"dimension {dimension.code} is required on {code}",
                )
            )
    return issues


def _column_issues(path: str, values: dict[str, str], columns: Sequence[Any]) -> list[Issue]:
    issues: list[Issue] = []
    ids = {c.column_id for c in columns}
    issues += [
        Issue("unknown_column", f"{path}/{name}", f"the template has no column {name}")
        for name in sorted(set(values) - ids)
    ]
    for column in columns:
        problem = value_problem(column, values.get(column.column_id))
        if problem:
            issues.append(Issue("invalid_value", f"{path}/{column.column_id}", f"{column.label} {problem}"))
    return issues


def journal_issues(
    draft: JournalDraft,
    *,
    template: JournalTemplate,
    chart: ChartOfAccounts,
    dimensions: DimensionSet | None,
    currency: str,
) -> list[Issue]:
    """Every problem with ``draft``: its currency against the template, its header and lines against the
    template, accounts against the chart and the journal's currency, dimensions against the dimension set, and
    the balance. ``currency`` is the entity's functional currency."""
    issues: list[Issue] = []
    held = draft.currency or currency
    if draft.currency is not None and draft.currency not in CURRENCY_EXPONENTS:
        issues.append(Issue("unknown_currency", "/currency", f"{draft.currency} is not a currency the platform knows"))
    elif draft.currency not in (None, currency, *template.currencies):
        allowed = ", ".join(sorted({currency, *template.currencies}))
        issues.append(
            Issue("currency_not_allowed", "/currency", f"template {template.template_id} allows only {allowed}")
        )
    if len(draft.description.strip()) < template.min_description_length:
        issues.append(
            Issue(
                "description_too_short",
                "/description",
                f"the description needs at least {template.min_description_length} characters",
            )
        )
    issues += _column_issues("/header", draft.header, template.header_columns)
    if len(draft.lines) < 2:
        issues.append(Issue("too_few_lines", "/lines", "a journal needs at least two lines"))
    if len(draft.lines) > template.max_lines:
        issues.append(Issue("too_many_lines", "/lines", f"the template allows at most {template.max_lines} lines"))
    for i, line in enumerate(draft.lines):
        path = f"/lines/{i}"
        account = chart.account(line.account)
        if account is None:
            issues.append(Issue("unknown_account", f"{path}/account", f"account {line.account} is not in the chart"))
            continue
        where = f"{path}/account"
        if account.status != "active" or not account.postable:
            issues.append(Issue("account_not_postable", where, f"account {line.account} is inactive or not postable"))
        if account.entities and draft.entity_id not in account.entities:
            issues.append(
                Issue("account_not_for_entity", where, f"account {line.account} is not used by {draft.entity_id}")
            )
        if account.currency is not None and account.currency != held:
            issues.append(Issue("account_currency", where, f"account {line.account} holds only {account.currency}"))
        if not template.allowed_accounts.allows(line.account, account.account_type):
            issues.append(
                Issue("account_not_allowed", where, f"template {template.template_id} doesn't allow {line.account}")
            )
        issues += _dimension_issues(path, line.dimensions, line.account, account.account_type, dimensions)
        issues += _column_issues(f"{path}/values", line.values, template.line_columns)
    if draft.total_debits() != draft.total_credits():
        issues.append(
            Issue(
                "unbalanced",
                "/lines",
                f"debits {draft.total_debits()} don't equal credits {draft.total_credits()}",
            )
        )
    return issues


def template_issues(template: JournalTemplate, chart: ChartOfAccounts) -> list[Issue]:
    """Accounts a template names must be in the chart."""
    return [
        Issue("unknown_account", "/allowed_accounts/codes", f"account {code} is not in the chart")
        for code in sorted(set(template.allowed_accounts.codes))
        if chart.account(code) is None
    ]


# ---------------------------------------------------------------------------------------------- risk and routing


@dataclass(frozen=True)
class RiskContext:
    """What the app looks up for a draft's risk score, stored with the score as its evidence."""

    submitted_on: date
    period_end: date  # the last day of the journal's period
    preparer_journals: int  # journals the preparer created in the window, not counting this one
    unused_accounts: tuple[str, ...]  # the draft's accounts with no approved line in the window
    suspense_account: str | None


@dataclass(frozen=True)
class Risk:
    score: int
    version: int
    factors: dict[str, dict[str, Any]] = field(default_factory=dict)  # factor -> hit, weight and inputs


def _round_or_repeating(amounts: Iterable[int], unit: int) -> dict[str, Any]:
    distinct = sorted(set(amounts))
    round_amounts = [a for a in distinct if a % unit == 0]
    endings: dict[int, list[int]] = {}
    for a in distinct:
        if a % 1000:
            endings.setdefault(a % 1000, []).append(a)
    repeated = sorted((e, xs) for e, xs in endings.items() if len(xs) >= 3)
    return {
        "hit": bool(round_amounts or repeated),
        "round_amounts": round_amounts,
        "repeated_endings": [e for e, _ in repeated],
    }


def risk_score(draft: JournalDraft, scoring: RiskScoring, ctx: RiskContext) -> Risk:
    """Score a draft on AS 2401.61's characteristics (a) to (e); see :class:`RiskScoring`."""
    late = ctx.submitted_on > ctx.period_end or (ctx.period_end - draft.accounting_date).days < scoring.period_end_days
    thin = len(draft.description.strip()) < scoring.thin_description_below
    sensitive = sorted(
        {
            ln.account
            for ln in draft.lines
            if ln.account in scoring.sensitive_accounts or ln.account == ctx.suspense_account
        }
    )
    evidence: dict[str, dict[str, Any]] = {
        "seldom_used_account": {
            "hit": bool(ctx.unused_accounts),
            "accounts": list(ctx.unused_accounts),
            "window_days": scoring.seldom_used_days,
        },
        "infrequent_preparer": {
            "hit": ctx.preparer_journals < scoring.infrequent_preparer_below,
            "journals_in_window": ctx.preparer_journals,
            "window_days": scoring.infrequent_preparer_days,
        },
        "period_end_thin_description": {
            "hit": late and thin,
            "late": late,
            "thin_description": thin,
            "period_end": ctx.period_end.isoformat(),
            "submitted_on": ctx.submitted_on.isoformat(),
        },
        "sensitive_account": {"hit": bool(sensitive), "accounts": sensitive},
        "round_amount": _round_or_repeating((ln.amount_minor for ln in draft.lines), scoring.round_amount_minor),
    }
    for name in RISK_FACTORS:
        evidence[name]["weight"] = scoring.weights[name]
    score = sum(d["weight"] for d in evidence.values() if d["hit"])
    return Risk(score=score, version=scoring.version, factors=evidence)


class RoutingError(ValueError):
    """The approval matrix can't route this journal (for example, it has no thresholds for its currency)."""


@dataclass(frozen=True)
class Routing:
    levels: int
    amount_levels: int
    risk_levels: int
    rule_levels: int
    rules: tuple[str, ...]
    total_minor: int


def _band(value: int, level_2_from: int, level_3_from: int) -> int:
    return 3 if value >= level_3_from else 2 if value >= level_2_from else 1


def route(draft: JournalDraft, *, currency: str, total_minor: int, matrix: ApprovalMatrix, risk: int) -> Routing:
    """The approval levels ``draft`` needs: the most that its total debits, its risk score or a rule asks for.

    ``currency`` is the entity's functional currency and ``total_minor`` the journal's total debits in it (its
    converted total for a foreign-currency journal), because the matrix's thresholds are per functional currency."""
    thresholds = matrix.thresholds_for(currency)
    if thresholds is None:
        raise RoutingError(f"the approval matrix has no thresholds for {currency}")
    total = total_minor
    amount_levels = _band(total, thresholds.level_2_from_minor, thresholds.level_3_from_minor)
    risk_levels = _band(risk, matrix.risk.level_2_from, matrix.risk.level_3_from)
    accounts = {ln.account for ln in draft.lines}
    matched = [
        r
        for r in matrix.rules
        if (not r.entities or draft.entity_id in r.entities)
        and (not r.templates or draft.template_id in r.templates)
        and (not r.accounts or accounts & set(r.accounts))
    ]
    rule_levels = max((r.min_levels for r in matched), default=1)
    return Routing(
        levels=max(amount_levels, risk_levels, rule_levels),
        amount_levels=amount_levels,
        risk_levels=risk_levels,
        rule_levels=rule_levels,
        rules=tuple(r.rule_id for r in matched),
        total_minor=total,
    )


def may_bypass(kind: Literal["recurring", "reversal"], total_minor: int, currency: str, matrix: ApprovalMatrix) -> bool:
    """Whether the matrix lets this journal skip approval: its kind is allowed, and it is within the cap."""
    allowed = matrix.bypass.recurring if kind == "recurring" else matrix.bypass.reversals
    cap = matrix.bypass.up_to_minor.get(currency)
    return allowed and cap is not None and total_minor <= cap


# ---------------------------------------------------------------------------------------------- conversion (D9)


@dataclass(frozen=True)
class Conversion:
    """A foreign-currency journal converted to its entity's functional currency, as a submission freezes it.

    Each line is converted exactly at the rate and rounded half-even (:func:`close_contracts.money.convert_minor`,
    the function the journaling engine uses). The residual goes on one rounding line to the chart's FX rounding
    account, so the functional debits equal the credits."""

    currency: str
    functional_currency: str
    rate: Rate
    lines: tuple[int, ...]  # each line's functional amount, in line order
    debits_minor: int  # functional, before the rounding line
    credits_minor: int
    rounding_account: str | None
    reused_from: str | None = None  # the journal whose rate a reversal reuses

    @property
    def rounding_side(self) -> Literal["DR", "CR"] | None:
        if self.debits_minor == self.credits_minor:
            return None
        return "CR" if self.debits_minor > self.credits_minor else "DR"

    @property
    def rounding_minor(self) -> int:
        return abs(self.debits_minor - self.credits_minor)

    @property
    def functional_total(self) -> int:
        """Total functional debits with the rounding line, equal to the credits."""
        return max(self.debits_minor, self.credits_minor)

    def as_json(self) -> dict[str, Any]:
        side = self.rounding_side
        return {
            "currency": self.currency,
            "functional_currency": self.functional_currency,
            "rate": self.rate.as_json(),
            "lines": [{"line_no": i, "functional_amount_minor": a} for i, a in enumerate(self.lines, start=1)],
            "rounding": None
            if side is None
            else {"side": side, "amount_minor": self.rounding_minor, "account": self.rounding_account},
            "functional_total_minor": self.functional_total,
            "reused_from": self.reused_from,
        }

    def hash(self) -> str:
        """What the submission's approvals are bound to, next to the content hash."""
        data: Any = self.as_json()
        return content_sha256(data)


def convert(
    draft: JournalDraft, *, functional: str, rate: Rate, rounding_account: str, reused_from: str | None = None
) -> Conversion:
    """Convert ``draft`` (in a currency other than ``functional``) at ``rate`` under D9."""
    if draft.currency is None or draft.currency == functional:
        raise ValueError("only a foreign-currency journal is converted")
    if (rate.key.from_currency, rate.key.to_currency) != (draft.currency, functional):
        raise ValueError(f"a {rate.key.label()} rate can't convert {draft.currency} to {functional}")
    amounts = tuple(convert_minor(ln.amount_minor, draft.currency, functional, rate.rate) for ln in draft.lines)
    return Conversion(
        currency=draft.currency,
        functional_currency=functional,
        rate=rate,
        lines=amounts,
        debits_minor=sum(a for a, ln in zip(amounts, draft.lines, strict=True) if ln.side == "DR"),
        credits_minor=sum(a for a, ln in zip(amounts, draft.lines, strict=True) if ln.side == "CR"),
        rounding_account=rounding_account,
        reused_from=reused_from,
    )
