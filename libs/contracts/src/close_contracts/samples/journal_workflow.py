"""SAMPLE journal workflow configuration, for testing only: not your company's approval matrix or templates.

The entities come from the generator's world, and the accounts from the sample chart
(``close_contracts.samples.journaling``). The real inputs are requested in ``docs/domain/stage-7-inputs.md``.

- **Entities:** US01 (USD), EU01 (EUR) and UK01 (GBP).
- **Templates:** a standard journal, an accrual that reverses into the next period, and an expense reclass.
- **The approval matrix:** level 2 from 10,000 and level 3 from 100,000 (8,000 and 80,000 in GBP), cash postings
  always at two levels, the AS 2401.61 risk score, and small recurring journals and automatic reversals allowed to
  bypass approval (flagged).
- **Recurring journals:** a monthly chargeback-reserve top-up in US01 that bypasses approval, and a quarterly
  dispute-fee accrual in EU01 that goes through it.
"""

from __future__ import annotations

from typing import Final

from close_contracts.config.journal_workflow import (
    AccountRange,
    AllowedAccounts,
    ApprovalMatrix,
    Bypass,
    Column,
    Entity,
    EntitySet,
    JournalTemplate,
    LevelThresholds,
    RecurringJournal,
    RecurringLine,
    Reminders,
    RiskScoring,
    RoutingRule,
)

SAMPLE_ENTITIES: Final = EntitySet(
    entity_set_id="sample_entities",
    entities=(
        Entity(entity_id="US01", name="Platform Inc. (US)", functional_currency="USD"),
        Entity(entity_id="EU01", name="Platform B.V. (EU)", functional_currency="EUR"),
        Entity(entity_id="UK01", name="Platform Ltd (UK)", functional_currency="GBP"),
    ),
)

_REFERENCE: Final = Column(column_id="reference", label="Reference", max_length=50)

SAMPLE_TEMPLATES: Final = (
    JournalTemplate(
        template_id="sample_standard_journal",
        name="Standard journal",
        description="Any balanced adjustment. An attachment is required from 50,000.",
        header_columns=(_REFERENCE,),
        attachment_required_from_minor=5_000_000,
        min_description_length=10,
    ),
    JournalTemplate(
        template_id="sample_accrual",
        name="Accrual (reverses next period)",
        description="An expense accrued against a liability, reversed on the first day of the next period.",
        header_columns=(
            Column(
                column_id="accrual_type",
                label="Accrual type",
                required=True,
                allowed_values=("carrier", "marketing", "fees", "reserve", "other"),
            ),
            _REFERENCE,
        ),
        allowed_accounts=AllowedAccounts(account_types=("expense",), ranges=(AccountRange(first="2310", last="2430"),)),
        min_description_length=15,
        auto_reverse=True,
    ),
    JournalTemplate(
        template_id="sample_reclass",
        name="Expense or revenue reclass",
        description="Moves an amount between expense or revenue accounts, with a reason on every line.",
        line_columns=(
            Column(
                column_id="reason_code",
                label="Reason",
                required=True,
                allowed_values=("misposting", "policy_change", "other"),
            ),
        ),
        allowed_accounts=AllowedAccounts(account_types=("expense", "revenue")),
        min_description_length=10,
    ),
    JournalTemplate(
        template_id="sample_fx_journal",
        name="Foreign-currency journal",
        description=(
            "A balanced adjustment entered in euros, pounds or dollars, converted to the entity's functional "
            "currency at the accounting date's daily spot rate when it is submitted (Stage 7c)."
        ),
        header_columns=(_REFERENCE,),
        min_description_length=10,
        currencies=("EUR", "GBP", "USD"),
    ),
)

SAMPLE_APPROVAL_MATRIX: Final = ApprovalMatrix(
    matrix_id="sample_approval_matrix",
    thresholds=(
        LevelThresholds(currency="USD", level_2_from_minor=1_000_000, level_3_from_minor=10_000_000),
        LevelThresholds(currency="EUR", level_2_from_minor=1_000_000, level_3_from_minor=10_000_000),
        LevelThresholds(currency="GBP", level_2_from_minor=800_000, level_3_from_minor=8_000_000),
    ),
    rules=(RoutingRule(rule_id="cash_needs_two", accounts=("1010",), min_levels=2),),
    risk=RiskScoring(
        weights={
            "seldom_used_account": 20,
            "infrequent_preparer": 15,
            "period_end_thin_description": 25,
            "sensitive_account": 20,
            "round_amount": 10,
        },
        level_2_from=40,
        level_3_from=70,
        sensitive_accounts=("1010", "4010", "4030", "4050"),
    ),
    reminders=Reminders(first_after_hours=24, repeat_every_hours=24, escalate_after=2),
    bypass=Bypass(
        recurring=True,
        reversals=True,
        up_to_minor={"USD": 500_000, "EUR": 500_000, "GBP": 400_000},
    ),
)

SAMPLE_RECURRING: Final = (
    RecurringJournal(
        recurring_id="sample_reserve_top_up",
        template_id="sample_standard_journal",
        entity_id="US01",
        frequency="monthly",
        day=28,
        first_period="2026-07",
        preparer="1001",  # alice in the dev stack (a user id, as the proxy sends it)
        description="Monthly chargeback reserve top-up",
        header={"reference": "RESERVE-TOPUP"},
        lines=(
            RecurringLine(account="6040", side="DR", amount_minor=250_000, description="Chargeback losses"),
            RecurringLine(account="2310", side="CR", amount_minor=250_000, description="Chargeback reserve"),
        ),
        auto_approve=True,
    ),
    RecurringJournal(
        recurring_id="sample_dispute_fee_accrual",
        template_id="sample_accrual",
        entity_id="EU01",
        frequency="quarterly",
        day=5,
        first_period="2026-07",
        preparer="1004",  # dave
        description="Quarterly accrual of payment provider dispute fees",
        header={"accrual_type": "fees"},
        lines=(
            RecurringLine(account="6050", side="DR", amount_minor=1_250_000, description="Dispute fees"),
            RecurringLine(account="2430", side="CR", amount_minor=1_250_000, description="Fees payable"),
        ),
    ),
)
