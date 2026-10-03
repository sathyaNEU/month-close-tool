"""The journal workflow's contracts (Gate 7a): templates, the approval matrix, recurring journals, and the pure
checks on a manual journal (validation, risk score, routing)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from close_contracts.config.journal_workflow import (
    ApprovalMatrix,
    Column,
    JournalTemplate,
    RecurringJournal,
    value_problem,
)
from close_contracts.journals import (
    JournalDraft,
    RiskContext,
    RoutingError,
    content_hash,
    from_recurring,
    journal_issues,
    may_bypass,
    reversal_of,
    risk_score,
    route,
    template_issues,
)
from close_contracts.samples.journal_workflow import (
    SAMPLE_APPROVAL_MATRIX,
    SAMPLE_ENTITIES,
    SAMPLE_RECURRING,
    SAMPLE_TEMPLATES,
)
from close_contracts.samples.journaling import SAMPLE_CHART, SAMPLE_DIMENSIONS

TEMPLATES = {t.template_id: t for t in SAMPLE_TEMPLATES}
STANDARD = TEMPLATES["sample_standard_journal"]


def draft(*lines: tuple[str, str, int], **fields: Any) -> JournalDraft:
    body: dict[str, Any] = {
        "entity_id": "US01",
        "template_id": "sample_standard_journal",
        "accounting_date": "2026-07-15",
        "description": "Reclass of a July carrier charge",
        "lines": [{"account": a, "side": s, "amount_minor": m} for a, s, m in lines],
    }
    return JournalDraft.model_validate(body | fields)


BALANCED = (("5020", "DR", 12_345), ("2410", "CR", 12_345))


def codes(d: JournalDraft, template: JournalTemplate = STANDARD, currency: str = "USD") -> set[str]:
    issues = journal_issues(d, template=template, chart=SAMPLE_CHART, dimensions=SAMPLE_DIMENSIONS, currency=currency)
    return {i.code for i in issues}


def test_every_sample_validates() -> None:
    assert [i for t in SAMPLE_TEMPLATES for i in template_issues(t, SAMPLE_CHART)] == []
    for r in SAMPLE_RECURRING:
        entity = SAMPLE_ENTITIES.entity(r.entity_id)
        assert entity is not None
        d = from_recurring(r, date(2026, 7, r.day))
        assert codes(d, TEMPLATES[r.template_id], entity.functional_currency) == set(), r.recurring_id
    assert SAMPLE_APPROVAL_MATRIX.thresholds_for("GBP") is not None


def test_column_rules() -> None:
    with pytest.raises(ValidationError, match="needs a default"):
        Column(column_id="x", label="X", read_only=True)
    with pytest.raises(ValidationError, match="non-negative"):
        Column(column_id="x", label="X", non_negative=True)
    with pytest.raises(ValidationError, match="allowed values"):
        Column(column_id="x", label="X", data_type="integer", allowed_values=("1",))
    with pytest.raises(ValidationError, match="reserved"):
        JournalTemplate(template_id="t_x", name="x", line_columns=(Column(column_id="account", label="A"),))
    cases: list[tuple[Column, str | None, str | None]] = [
        (Column(column_id="x", label="X", required=True), None, "is required"),
        (Column(column_id="x", label="X", required=True, default="d"), None, None),
        (Column(column_id="x", label="X", read_only=True, default="A"), "B", "is read-only (always 'A')"),
        (Column(column_id="x", label="X", allowed_values=("a", "b")), "c", "must be one of a, b"),
        (Column(column_id="x", label="X", data_type="date"), "2026-13-01", "is not a date (YYYY-MM-DD)"),
        (Column(column_id="x", label="X", data_type="decimal"), "1.5e", "is not a number"),
        (Column(column_id="x", label="X", data_type="decimal"), "NaN", "is not a finite number"),
        (Column(column_id="x", label="X", data_type="integer"), "1.5", "is not an integer"),
        (Column(column_id="x", label="X", data_type="integer", non_negative=True), "-3", "must not be negative"),
        (Column(column_id="x", label="X", data_type="boolean"), "yes", "is not true or false"),
        (Column(column_id="x", label="X", max_length=3), "abcd", "is longer than 3 characters"),
        (Column(column_id="x", label="X", data_type="decimal", non_negative=True), "0.00", None),
    ]
    for column, value, expected in cases:
        assert value_problem(column, value) == expected, (column, value)


def test_journal_validation_codes() -> None:
    assert codes(draft(*BALANCED)) == set()
    assert codes(draft(("5020", "DR", 10), ("2410", "CR", 9))) == {"unbalanced"}
    assert codes(draft(("5020", "DR", 10))) == {"too_few_lines", "unbalanced"}
    assert codes(draft(("0000", "DR", 10), ("2410", "CR", 10))) == {"unknown_account"}
    assert codes(draft(*BALANCED, description="short")) == {"description_too_short"}
    assert codes(draft(*BALANCED, header={"nope": "x"})) == {"unknown_column"}
    reclass = TEMPLATES["sample_reclass"]
    no_reason = draft(("6030", "DR", 10), ("5030", "CR", 10), template_id="sample_reclass")
    assert codes(no_reason, reclass) == {"invalid_value"}
    assert codes(draft(("6030", "DR", 10), ("2410", "CR", 10), template_id="sample_reclass"), reclass) >= {
        "account_not_allowed"
    }
    # Revenue lines need the program dimension; PSP clearing needs the provider.
    revenue = draft(("4010", "CR", 10), ("5020", "DR", 10))
    assert codes(revenue) == {"missing_dimension"}
    tagged = draft(("4010", "CR", 10), ("5020", "DR", 10))
    lines = [ln.model_copy(update={"dimensions": {"program": "listings"}}) for ln in tagged.lines]
    assert codes(tagged.model_copy(update={"lines": tuple(lines)})) == set()
    wrong = [ln.model_copy(update={"dimensions": {"program": "no_such"}}) for ln in tagged.lines]
    assert codes(tagged.model_copy(update={"lines": tuple(wrong)})) == {"invalid_dimension_value"}
    unknown = [ln.model_copy(update={"dimensions": {"colour": "red"}}) for ln in draft(*BALANCED).lines]
    assert codes(draft(*BALANCED).model_copy(update={"lines": tuple(unknown)})) == {"unknown_dimension"}
    small = STANDARD.model_copy(update={"max_lines": 2})
    assert codes(draft(*BALANCED, ("5020", "DR", 1), ("2410", "CR", 1)), small) == {"too_many_lines"}


def test_accounts_must_suit_the_entity_and_its_currency() -> None:
    accounts = [a for a in SAMPLE_CHART.accounts if a.code != "5020"]
    euro_only = next(a for a in SAMPLE_CHART.accounts if a.code == "5020").model_copy(
        update={"entities": ("EU01",), "currency": "EUR"}
    )
    chart = SAMPLE_CHART.model_copy(update={"accounts": (*accounts, euro_only)})
    issues = journal_issues(draft(*BALANCED), template=STANDARD, chart=chart, dimensions=None, currency="USD")
    assert {i.code for i in issues} == {"account_not_for_entity", "account_currency"}
    closed = SAMPLE_CHART.model_copy(
        update={
            "accounts": (*accounts, euro_only.model_copy(update={"entities": (), "currency": None, "postable": False}))
        }
    )
    issues = journal_issues(draft(*BALANCED), template=STANDARD, chart=closed, dimensions=None, currency="USD")
    assert {i.code for i in issues} == {"account_not_postable"}


CTX = RiskContext(
    submitted_on=date(2026, 7, 15),
    period_end=date(2026, 7, 31),
    preparer_journals=10,
    unused_accounts=(),
    suspense_account="9999",
)


def hits(d: JournalDraft, ctx: RiskContext = CTX) -> set[str]:
    risk = risk_score(d, SAMPLE_APPROVAL_MATRIX.risk, ctx)
    assert risk.score == sum(f["weight"] for f in risk.factors.values() if f["hit"])
    return {name for name, f in risk.factors.items() if f["hit"]}


def test_each_risk_factor() -> None:
    assert hits(draft(*BALANCED)) == set()
    assert hits(draft(*BALANCED), CTX.__class__(**{**CTX.__dict__, "unused_accounts": ("2410",)})) == {
        "seldom_used_account"
    }
    assert hits(draft(*BALANCED), CTX.__class__(**{**CTX.__dict__, "preparer_journals": 0})) == {"infrequent_preparer"}
    late = draft(*BALANCED, accounting_date="2026-07-30", description="July accrual")
    assert hits(late) == {"period_end_thin_description"}
    assert hits(draft(*BALANCED, accounting_date="2026-07-30")) == set()  # late, but well described
    post_close = CTX.__class__(**{**CTX.__dict__, "submitted_on": date(2026, 8, 2)})
    assert hits(draft(*BALANCED, description="Accrual"), post_close) == {"period_end_thin_description"}
    assert hits(draft(("9999", "DR", 12_345), ("2410", "CR", 12_345))) == {"sensitive_account"}
    assert hits(draft(("1010", "DR", 12_345), ("2410", "CR", 12_345))) == {"sensitive_account"}
    assert hits(draft(("5020", "DR", 300_000), ("2410", "CR", 300_000))) == {"round_amount"}
    repeating = draft(("5020", "DR", 1_999), ("5020", "DR", 2_999), ("5020", "DR", 3_999), ("2410", "CR", 8_997))
    assert hits(repeating) == {"round_amount"}
    evidence = risk_score(late, SAMPLE_APPROVAL_MATRIX.risk, CTX).factors["period_end_thin_description"]
    assert (evidence["late"], evidence["thin_description"], evidence["weight"]) == (True, True, 25)


def test_routing_takes_the_most_any_input_asks_for() -> None:
    matrix = SAMPLE_APPROVAL_MATRIX
    table = [  # (debit total in minor units, risk score, accounts, expected levels)
        (999_999, 0, "5020", 1),
        (1_000_000, 0, "5020", 2),
        (10_000_000, 0, "5020", 3),
        (100, 39, "5020", 1),
        (100, 40, "5020", 2),
        (100, 70, "5020", 3),
        (100, 0, "1010", 2),  # the cash rule
        (10_000_000, 0, "1010", 3),
    ]
    for total, risk, account, expected in table:
        d = draft((account, "DR", total), ("2410", "CR", total))
        assert route(d, currency="USD", total_minor=d.total_debits(), matrix=matrix, risk=risk).levels == expected, (
            total,
            risk,
            account,
        )
    gbp = draft(("5020", "DR", 800_000), ("2410", "CR", 800_000))
    assert route(gbp, currency="GBP", total_minor=gbp.total_debits(), matrix=matrix, risk=0).levels == 2
    small = draft(("1010", "DR", 100), ("2410", "CR", 100))
    routed = route(small, currency="USD", total_minor=small.total_debits(), matrix=matrix, risk=0)
    assert (routed.rules, routed.rule_levels, routed.total_minor) == (("cash_needs_two",), 2, 100)
    with pytest.raises(RoutingError, match="JPY"):
        route(draft(*BALANCED), currency="JPY", total_minor=12_345, matrix=matrix, risk=0)


def test_bypass_needs_the_kind_allowed_and_a_cap() -> None:
    m = SAMPLE_APPROVAL_MATRIX
    assert may_bypass("recurring", 500_000, "USD", m)
    assert not may_bypass("recurring", 500_001, "USD", m)
    assert not may_bypass("recurring", 1, "JPY", m)  # no cap for that currency
    closed = m.model_copy(update={"bypass": m.bypass.model_copy(update={"reversals": False})})
    assert not may_bypass("reversal", 1, "USD", closed)


def test_matrix_rules() -> None:
    body = SAMPLE_APPROVAL_MATRIX.model_dump(mode="json")
    with pytest.raises(ValidationError, match="missing"):
        ApprovalMatrix.model_validate(body | {"risk": body["risk"] | {"weights": {"round_amount": 1}}})
    thresholds = [body["thresholds"][0] | {"level_3_from_minor": 1}]
    with pytest.raises(ValidationError, match="level 3 must start"):
        ApprovalMatrix.model_validate(body | {"thresholds": thresholds})
    with pytest.raises(ValidationError, match="one threshold row per currency"):
        ApprovalMatrix.model_validate(body | {"thresholds": [body["thresholds"][0]] * 2})


def test_recurring_due_dates_and_balance() -> None:
    monthly, quarterly = SAMPLE_RECURRING
    assert [monthly.due(p) for p in ("2026-06", "2026-07", "2026-08")] == [False, True, True]
    assert [quarterly.due(p) for p in ("2026-07", "2026-08", "2026-09", "2026-10")] == [True, False, False, True]
    ended = monthly.model_copy(update={"last_period": "2026-08"})
    assert not ended.due("2026-09")
    body = monthly.model_dump(mode="json")
    body["lines"][0]["amount_minor"] += 1
    with pytest.raises(ValidationError, match="don't balance"):
        RecurringJournal.model_validate(body)


def test_content_hash_and_reversal() -> None:
    d = draft(*BALANCED)
    assert content_hash(d) == content_hash(draft(*BALANCED))
    assert content_hash(d) != content_hash(draft(*BALANCED, description="Something else entirely"))
    back = reversal_of(d, date(2026, 8, 1), "Reversal of J-1")
    assert [(ln.account, ln.side) for ln in back.lines] == [("5020", "CR"), ("2410", "DR")]
    assert back.accounting_date == date(2026, 8, 1)
