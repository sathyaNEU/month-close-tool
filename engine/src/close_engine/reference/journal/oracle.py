"""The journal oracle: the sample accounting written directly in Python, independent of the rule engine.

The engine books journals from configuration: event types, automated journal definitions and account mappings.
This module books the same sample accounting from the policy answers alone
(``docs/domain/sample-journal-rules.md`` is the specification both follow). It never reads the definitions or
the mappings, and it converts currencies with its own code. Gate 6 holds the engine to it for every
combination of the policy switches.

Both follow the same rules at the edges: a zero amount writes no line, a negative amount posts its absolute
value on the other side to suspense, each line is converted on its own (D9), and each event's residual goes on
one FX rounding line.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from close_contracts.config.accounting_policy import AccountingPolicy, ProgramPolicy
from close_engine.reference.journal.events import Event
from close_engine.reference.journal.posting import ReferenceData

SUSPENSE, ROUNDING = "9999", "7010"
Key = tuple[str, int, str, str, tuple[tuple[str, str], ...]]  # event id, version, account, currency, dimensions


@dataclass(frozen=True)
class Expected:
    account: str
    side: str
    amount: int
    dimensions: tuple[tuple[str, str], ...] = ()


def _psp(policy: AccountingPolicy) -> str:
    return {"cash_equivalent": "1110", "accounts_receivable": "1120"}[policy.presentation.psp_receivable]


def _fees(policy: AccountingPolicy) -> str:
    return {"cost_of_revenue": "5030", "sga": "6030"}[policy.presentation.processing_fees]


def _chargebacks(policy: AccountingPolicy) -> str:
    return {"reserve": "2310", "transaction_loss": "6040"}[policy.presentation.chargebacks]


def _program(policy: AccountingPolicy, program: str | None) -> ProgramPolicy | None:
    return next((p for p in policy.programs if p.program.value == program), None)


def _revenue(f: Mapping[str, Any], p: ProgramPolicy, *, reverse: bool) -> list[Expected]:
    """Revenue and the seller payable at the trigger (or, reversed, after a full refund)."""
    prog = (("program", str(f["program"])),)
    net = f["amount_item_price"] - f["amount_seller_fee"] - f["amount_processing_fee"]
    cap = (  # revenue from the buyer on this order
        f["amount_buyer_fee"]
        + (f["amount_shipping"] if p.shipping.value == "gross" else 0)
        + (f["amount_item_price"] if p.model.value == "owned_inventory" else 0)
    )
    incentive = f["amount_incentive"]
    contra = {
        "contra_revenue": incentive,
        "marketing": 0,
        "contra_up_to_cumulative_then_marketing": min(incentive, cap),
    }[p.incentives.value]
    credit, debit = ("DR", "CR") if reverse else ("CR", "DR")
    out = []
    if p.model.value == "agent":
        out += [
            Expected("2010", credit, net),
            Expected("4010", credit, f["amount_seller_fee"], prog),
            Expected("4020", credit, f["amount_processing_fee"], prog),
        ]
    else:
        out += [
            Expected("4050", credit, f["amount_item_price"], prog),
            Expected("5010", debit, net),
            Expected("2010", credit, net),
        ]
    shipping = {"gross": "4040", "net_as_agent": "5021"}[p.shipping.value]
    out += [
        Expected("4030", credit, f["amount_buyer_fee"], prog),
        Expected(shipping, credit, f["amount_shipping"], prog),
        Expected("4090", debit, contra, prog),
        Expected("6010", debit, incentive - contra, prog),
    ]
    return out


def _order_lines(event: Event, policy: AccountingPolicy) -> list[Expected]:
    f, t = event.fields, event.event_type
    p = _program(policy, f.get("program"))
    psp = _psp(policy)
    psp_dim = (("psp", str(f["attr_psp"])),)
    deposit = f["amount_card"] + f["amount_store_credit"] - f["amount_tax"]
    if t == "ORDER_CAPTURED":
        out = [
            Expected(psp, "DR", f["amount_card"], psp_dim),
            Expected("2210", "DR", f["amount_store_credit"]),
            Expected("2110", "CR", f["amount_tax"]),
            Expected("2020", "CR", deposit),
        ]
        if p is not None and p.trigger.value == "payment":
            out += [Expected("2020", "DR", deposit), *_revenue(f, p, reverse=False)]
        return out
    if t == "ORDER_STAGE":
        if p is None or f["attr_stage"] != p.trigger.value:
            return []
        return [Expected("2020", "DR", deposit), *_revenue(f, p, reverse=False)]
    kind, prog = f["attr_kind"], (("program", str(f["program"])),)
    refund = f["amount_refund"]
    if kind in ("cancel", "verification_failed"):
        common = [
            Expected("2110", "DR", f["amount_tax"]),
            Expected(psp, "CR", refund, psp_dim),
            Expected("2210", "CR", f["amount_store_credit"]),
        ]
        if p is not None and p.trigger.value == "payment":
            return common + _revenue(f, p, reverse=True)
        return [Expected("2020", "DR", deposit), *common]
    if kind == "partial":
        credit = Expected(psp, "CR", refund, psp_dim) if f["attr_method"] == "card" else None
        store = Expected("2210", "CR", refund) if f["attr_method"] == "store_credit" else None
        return [Expected("4095", "DR", refund, prog), *(x for x in (credit, store) if x is not None)]
    if kind == "clawback":
        net = f["amount_item_price"] - f["amount_seller_fee"] - f["amount_processing_fee"]
        return [
            Expected("2010", "DR", net),
            Expected("4095", "DR", refund - net, prog),
            Expected(psp, "CR", refund, psp_dim),
        ]
    return []


def expected_lines(event: Event, policy: AccountingPolicy) -> list[Expected]:
    f, t = event.fields, event.event_type
    if t in ("ORDER_CAPTURED", "ORDER_STAGE", "ORDER_REFUND"):
        return _order_lines(event, policy)
    psp_dim = (("psp", str(f.get("attr_psp"))),)
    if t == "SELLER_CHARGE":
        return [
            Expected("2010", "DR", f["amount_penalty"]),
            Expected("4060", "CR", f["amount_penalty"], (("program", str(f["program"])),)),
        ]
    if t == "PSP_FEE":
        return [Expected(_fees(policy), "DR", f["amount_fee"]), Expected(_psp(policy), "CR", f["amount_fee"], psp_dim)]
    if t == "PSP_FEE_BILLED":
        return [Expected(_fees(policy), "DR", f["amount_fee"]), Expected("2430", "CR", f["amount_fee"])]
    if t == "PSP_DISPUTE":
        amount, fee, total = f["amount_amount"], f["amount_fee"], f["amount_amount"] + f["amount_fee"]
        if f["attr_direction"] == "lost":
            return [
                Expected(_chargebacks(policy), "DR", amount),
                Expected("6050", "DR", fee),
                Expected(_psp(policy), "CR", total, psp_dim),
            ]
        return [
            Expected(_psp(policy), "DR", total, psp_dim),
            Expected(_chargebacks(policy), "CR", amount),
            Expected("6050", "CR", fee),
        ]
    two = {
        "PSP_PAYOUT": ("1010", _psp(policy), psp_dim),
        "SELLER_PAYOUT": ("2010", "1130", ()),
        "PAYOUT_FUNDING": ("1130", "1010", ()),
        "SHIPPING_LABEL": ("5020", "2410", ()),
        "TAX_REMITTED": ("2110", "1010", ()),
    }
    if t in two:
        dr, cr, dims = two[t]
        return [Expected(dr, "DR", f["amount_amount"]), Expected(cr, "CR", f["amount_amount"], dims)]
    if t == "CARRIER_CHARGE":
        account = "2410" if f["attr_charge_type"] == "transportation" else "5020"
        return [Expected(account, "DR", f["amount_amount"]), Expected("2420", "CR", f["amount_amount"])]
    raise ValueError(f"the oracle knows no event type {t}")


def _finish(lines: Iterable[Expected]) -> list[Expected]:
    """Drop zero lines; a negative amount posts its absolute value on the other side, to suspense."""
    out = []
    for ln in lines:
        if ln.amount == 0:
            continue
        flipped = Expected(SUSPENSE, "CR" if ln.side == "DR" else "DR", -ln.amount, ln.dimensions)
        out.append(flipped if ln.amount < 0 else ln)
    return out


def _convert(amount: int, currency: str, functional: str, day: Any, ref: ReferenceData) -> int:
    if currency == functional:
        return amount
    rate, _ = ref.rates[(day, currency, functional)]
    scale = Decimal(10) ** (ref.exponents.get(functional, 2) - ref.exponents.get(currency, 2))
    return int((Decimal(amount) * rate * scale).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def oracle_nets(
    events: Iterable[Event], policy: AccountingPolicy, ref: ReferenceData, version: int = 1
) -> tuple[dict[Key, int], dict[Key, int]]:
    """Expected net amounts (debit positive) per (event, version, account, currency, dimensions), in the
    transaction currency and in the functional currency."""
    tx: dict[Key, int] = defaultdict(int)
    fn: dict[Key, int] = defaultdict(int)
    for event in events:
        lines = _finish(expected_lines(event, policy))
        if not lines:
            continue
        currency, entity, day = (
            str(event.fields["currency"]),
            str(event.fields["entity_id"]),
            event.fields["accounting_date"],
        )
        functional = ref.functional[entity]
        residual = 0
        for ln in lines:
            sign = 1 if ln.side == "DR" else -1
            key = (event.event_id, version, ln.account, currency, tuple(sorted(ln.dimensions)))
            tx[key] += sign * ln.amount
            converted = _convert(ln.amount, currency, functional, day, ref)
            fn[key] += sign * converted
            residual += sign * converted
        if residual:
            fn[(event.event_id, version, ROUNDING, currency, ())] -= residual
    return _nonzero(tx), _nonzero(fn)


def engine_nets(lines: Iterable[Any]) -> tuple[dict[Key, int], dict[Key, int]]:
    tx: dict[Key, int] = defaultdict(int)
    fn: dict[Key, int] = defaultdict(int)
    for ln in lines:
        sign = 1 if ln.side == "DR" else -1
        key = (ln.event_id, ln.event_version, ln.account, ln.currency, tuple(sorted(ln.dimensions)))
        tx[key] += sign * ln.amount_minor
        fn[key] += sign * ln.functional_amount_minor
    return _nonzero(tx), _nonzero(fn)


def _nonzero(nets: Mapping[Key, int]) -> dict[Key, int]:
    return {k: v for k, v in nets.items() if v}
