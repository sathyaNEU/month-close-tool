"""The policy-switch matrix (Gate 6): every combination of one program's switches, with the other programs at
their base answers, and every combination of the presentation switches. ``flips`` changes one switch at a time,
for the guard that each switch really changes the books on the test data."""

from __future__ import annotations

from itertools import product
from typing import Any

from close_contracts.config.accounting_policy import (
    AccountingPolicy,
    IncentiveTreatment,
    Program,
    ProgramModel,
    ShippingTreatment,
    Trigger,
)

PRESENTATION: dict[str, tuple[str, ...]] = {
    "psp_receivable": ("cash_equivalent", "accounts_receivable"),
    "processing_fees": ("cost_of_revenue", "sga"),
    "chargebacks": ("reserve", "transaction_loss"),
}


def _triggers(program: Program) -> list[str]:
    return [t.value for t in Trigger if t != Trigger.VERIFICATION_PASSED or program == Program.VERIFIED_MARKETPLACE]


def _with(base: AccountingPolicy, program: str | None = None, **changes: Any) -> AccountingPolicy:
    data = base.model_dump(mode="json")
    if program is None:
        data["presentation"] |= changes
    else:
        data["programs"] = [p | changes if p["program"] == program else p for p in data["programs"]]
    return AccountingPolicy.model_validate(data)


def program_matrix(base: AccountingPolicy) -> list[tuple[str, AccountingPolicy]]:
    out = []
    for p in base.programs:
        for trigger, model, shipping, incentives in product(
            _triggers(p.program), [m.value for m in ProgramModel], [s.value for s in ShippingTreatment],
            [i.value for i in IncentiveTreatment],
        ):  # fmt: skip
            label = f"{p.program.value}: {trigger}, {model}, {shipping}, {incentives}"
            policy = _with(
                base, p.program.value, trigger=trigger, model=model, shipping=shipping, incentives=incentives
            )
            out.append((label, policy))
    return out


def presentation_matrix(base: AccountingPolicy) -> list[tuple[str, AccountingPolicy]]:
    names = list(PRESENTATION)
    return [
        (f"presentation: {', '.join(values)}", _with(base, None, **dict(zip(names, values, strict=True))))
        for values in product(*PRESENTATION.values())
    ]


def flips(base: AccountingPolicy) -> list[tuple[str, AccountingPolicy]]:
    """Every alternative value of every switch, one switch at a time."""
    out: list[tuple[str, AccountingPolicy]] = []
    for p in base.programs:
        options = {
            "trigger": _triggers(p.program),
            "model": [m.value for m in ProgramModel],
            "shipping": [s.value for s in ShippingTreatment],
            "incentives": [i.value for i in IncentiveTreatment],
        }
        current = {
            "trigger": p.trigger.value,
            "model": p.model.value,
            "shipping": p.shipping.value,
            "incentives": p.incentives.value,
        }
        out.extend(
            (f"{p.program.value}.{switch}={value}", _with(base, p.program.value, **{switch: value}))
            for switch, values in options.items()
            for value in values
            if value != current[switch]
        )
    presentation = base.presentation.model_dump()
    out.extend(
        (f"presentation.{switch}={value}", _with(base, None, **{switch: value}))
        for switch, values in PRESENTATION.items()
        for value in values
        if value != presentation[switch]
    )
    return out
