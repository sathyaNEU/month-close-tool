"""SAMPLE close configuration, for testing only: not your company's close checklist or flux thresholds.

The users are the local development users (alice 1001 prepares US01 journals, dave 1004 is US01's controller and
level 1 approver, erin 1005 its level 2 approver), so the manual tasks are US01's. The automatic tasks apply to
every entity. The real close calendar is requested in ``docs/domain/stage-7-inputs.md``.

- **The checklist:** a shipping-label cut-off on the last business day of the period (BD-1); then, after the
  period end, journals settled, a carrier accrual and a seller-payables review, suspense cleared, the trial balance
  balanced, the GL tied to the ERP, the flux explained and reviewed, and the controller's sign-off.
- **The flux rules:** explain a change of at least 10,000 and 10% on a balance-sheet account (8,000 in GBP), at
  least 5,000 and 10% on an income-statement account (4,000 in GBP), and any change at all on the suspense account.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from close_contracts.config.close import CloseChecklist, CloseTask, FluxRules, FluxThreshold, TaskReminders

ALICE, DAVE, ERIN = "1001", "1004", "1005"

SAMPLE_CHECKLIST: Final = CloseChecklist(
    checklist_id="sample_month_end_close",
    name="Month-end close (sample)",
    tasks=(
        CloseTask(
            task_id="shipping_label_cutoff",
            name="Cut off shipping labels",
            instructions="Confirm every label bought in the period is in the carrier extract before the period ends.",
            due_day=-1,
            preparer=ALICE,
            reviewer=DAVE,
            entities=("US01",),
        ),
        CloseTask(
            task_id="journals_settled",
            name="Every journal approved and posted",
            instructions="No journal dated in the period is waiting for approval or posting.",
            due_day=1,
            check="journals_settled",
        ),
        CloseTask(
            task_id="carrier_accrual",
            name="Book the carrier cost accrual",
            instructions="Accrue carrier invoices not yet received, from the label count (template sample_accrual).",
            due_day=1,
            preparer=ALICE,
            reviewer=ERIN,
            entities=("US01",),
        ),
        CloseTask(
            task_id="seller_payables_review",
            name="Review seller payables",
            instructions="Compare seller payables (2010) with the payout schedule and explain any gap.",
            due_day=2,
            depends_on=("journals_settled",),
            preparer=ALICE,
            reviewer=DAVE,
            entities=("US01",),
        ),
        CloseTask(
            task_id="suspense_clear",
            name="Suspense account cleared",
            instructions="The suspense account (9999) has no balance for the period.",
            due_day=2,
            depends_on=("journals_settled",),
            check="suspense_clear",
        ),
        CloseTask(
            task_id="gl_balanced",
            name="Trial balance balances",
            due_day=2,
            depends_on=("journals_settled",),
            check="gl_balanced",
        ),
        CloseTask(
            task_id="erp_ties",
            name="The Databricks GL ties to the ERP",
            instructions="Upload the ERP's trial balance first; every account must tie.",
            due_day=3,
            depends_on=("gl_balanced", "suspense_clear"),
            check="erp_ties",
        ),
        CloseTask(
            task_id="flux_reviewed",
            name="Flux explained and reviewed",
            due_day=4,
            depends_on=("erp_ties",),
            check="flux_reviewed",
        ),
        CloseTask(
            task_id="controller_sign_off",
            name="Controller's close sign-off",
            instructions="Every task is done and the period can close.",
            due_day=5,
            depends_on=(
                "shipping_label_cutoff",
                "carrier_accrual",
                "seller_payables_review",
                "flux_reviewed",
            ),
            preparer=DAVE,
            reviewer=ERIN,
            entities=("US01",),
        ),
    ),
    reminders=TaskReminders(escalate_after_days=2),
)

SAMPLE_FLUX_RULES: Final = FluxRules(
    rules_id="sample_flux_rules",
    by_type={
        kind: FluxThreshold(amounts={"USD": 1_000_000, "EUR": 1_000_000, "GBP": 800_000}, percent=Decimal(10))
        for kind in ("asset", "liability", "equity")
    }
    | {
        kind: FluxThreshold(amounts={"USD": 500_000, "EUR": 500_000, "GBP": 400_000}, percent=Decimal(10))
        for kind in ("revenue", "expense")
    },
    accounts={"9999": FluxThreshold(amounts={"USD": 0, "EUR": 0, "GBP": 0}, percent=Decimal(0), operator="or")},
)
